# Standard
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
import os
import time

# Third Party
from tqdm import tqdm

# Local
from fms_sdg.base.databuilder import DataBuilder
from fms_sdg.base.registry import get_data_builder
from fms_sdg.base.task import SdgTask
from fms_sdg.databuilders import DataBuilderIndex
import fms_sdg.utils as utils

sdg_logger = utils.sdg_logger


def generate_data(
    max_gen_requests: int,
    data_path: str,
    output_dir: str,
    task_kwargs: Dict,
    builder_kwargs: Dict,
    include_data_path: Optional[str] = None,
    include_builder_path: Optional[str] = None,
    restart_generation: bool = False,
):
    # TODO: better naming convention...
    name = (
        Path(os.path.split(data_path)[0]).stem
        if os.path.isfile(data_path)
        else Path(data_path).stem
    )
    output_dir = os.path.join(output_dir, name)

    # check data_path first then seed_tasks_path
    # throw an error if both not found
    # pylint: disable=broad-exception-caught,raise-missing-from
    if data_path and os.path.exists(data_path):
        task_inits = utils.read_data(data_path, include_data_path)
    else:
        raise SystemExit(f"Error: data path ({data_path}) does not exist.")

    # gather data builders here
    builder_list = [t["data_builder"] for t in task_inits]
    builder_index = DataBuilderIndex(include_path=include_builder_path)
    builder_names = builder_index.match_builders(builder_list)
    for builder in [
        builder for builder in builder_list if builder not in builder_names
    ]:
        if os.path.isfile(builder):
            config = utils.load_yaml_config(builder)
            builder_names.append(config)

    builder_missing = set(
        [
            builder
            for builder in builder_list
            if builder not in builder_names and "*" not in builder
        ]
    )

    if builder_missing:
        missing = ", ".join(builder_missing)
        raise ValueError(f"Builder specifications not found: [{missing}]")

    progress_bar = tqdm(total=len(task_inits), desc="Running generation tasks")
    total_discarded = 0
    generate_start = time.time()

    for builder_name, builder_cfg in builder_index.load_builder_configs(
        builder_names
    ).items():
        # we batch together tasks at the level of data builders
        original_builder_info = builder_index.builder_index[builder_name][0]
        if isinstance(builder_cfg, tuple):
            _, builder_cfg = builder_cfg
            if builder_cfg is None:
                continue

        # builder_dir is stored in the first builder_info in the list
        utils.import_builder(original_builder_info["builder_dir"])

        data_builder: DataBuilder = get_data_builder(builder_name)(
            config=builder_cfg,
            output_dir=output_dir,
            **builder_kwargs,
        )

        tasks: List[SdgTask] = [
            data_builder.TASK_TYPE(output_dir=output_dir, **task_init, **task_kwargs)
            for task_init in task_inits
            if task_init["data_builder"] == builder_name
        ]

        seeds = len([s for task in tasks for s in task.seed_data])
        sdg_logger.debug(f"Loaded {seeds} human-written seed examples from {data_path}")
        if not seeds:
            raise SystemExit("Nothing to generate. Exiting.")

        date_suffix = (
            datetime.now().replace(microsecond=0).isoformat().replace(":", "_")
        )
        output_file_discarded = os.path.join(
            output_dir, f"discarded_{name}_{date_suffix}.log"
        )

        if not os.path.exists(output_dir):
            os.makedirs(output_dir, exist_ok=True)

        # load the LM-generated data
        for task in tasks:
            if restart_generation:
                task.clear_data()
            if os.path.exists(task.output_path):
                task_data = task.load_data()
                task.machine_data = task_data
                sdg_logger.debug(f"Loaded {len(task_data)} machine-generated data")

        completed_tasks = [task for task in tasks if task.is_complete()]
        tasks = [task for task in tasks if task not in completed_tasks]

        request_idx = 0
        while tasks and request_idx <= max_gen_requests:
            request_idx += 1

            iter_discarded = 0

            data_pool = [
                e for task in tasks for e in (task.seed_data + task.machine_data)
            ]

            filtered_data, discarded = data_builder(
                request_idx,
                data_pool,
            )
            for task in tasks:
                new_data = [fid for fid in filtered_data if fid.task_name == task.name]
                task.machine_data.extend(new_data)
                if task.is_complete():
                    completed_tasks.append(task)
                    progress_bar.update()
                task.save_data(new_data)

            iter_discarded += discarded

            tasks = [task for task in tasks if task not in completed_tasks]

            total_discarded += iter_discarded
            sdg_logger.info(
                f"Generated {sum([len(task.machine_data) for task in tasks + completed_tasks])} data (discarded {iter_discarded})"
            )

        # TODO: cleanup
        del data_builder

    progress_bar.close()

    if total_discarded:
        sdg_logger.info(
            f"{total_discarded} discarded due to format (see {output_file_discarded})"
        )

    generate_duration = time.time() - generate_start
    sdg_logger.info(f"Generation took {generate_duration:.2f}s")
