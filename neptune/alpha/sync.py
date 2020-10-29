#
# Copyright (c) 2020, Neptune Labs Sp. z o.o.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import argparse
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from functools import partial
from itertools import filterfalse
from pathlib import Path
from typing import Collection, Iterable, List, Optional, Tuple, Any

from bravado.exception import HTTPError

from neptune.alpha.constants import NEPTUNE_EXPERIMENT_DIRECTORY, OPERATIONS_DISK_QUEUE_PREFIX, OFFLINE_DIRECTORY
from neptune.alpha.envs import PROJECT_ENV_NAME
from neptune.alpha.exceptions import ProjectNotFound
from neptune.alpha.internal.backends.api_model import Project
from neptune.alpha.internal.backends.hosted_neptune_backend import HostedNeptuneBackend
from neptune.alpha.internal.backends.neptune_backend import NeptuneBackend
from neptune.alpha.internal.containers.disk_queue import DiskQueue
from neptune.alpha.internal.credentials import Credentials
from neptune.alpha.internal.operation import VersionedOperation
from neptune.alpha.internal.utils.sync_offset_file import SyncOffsetFile


#######################################################################################################################
# Argument parser
#######################################################################################################################


epilog = """
Neptune stores experiment data on disk. In case an experiment is running offline
or network is unavailable as the experiment runs, experiment data can be synchronized
with the server with this utility.

If you run experiments from directory D, then Neptune stores experiment data in
'D/.neptune' folder. You can specify any directory which contains a '.neptune' folder
for synchronization.

Examples:

  # Synchronize all experiments in the current directory
  python -m neptune.alpha.sync

  # Synchronize all experiments in the given location
  python -m neptune.alpha.sync --location foo/bar

  # Synchronize only experiments "NPT-42" and "NPT-43" in "workspace/project" in the current directory
  python -m neptune.alpha.sync --experiment workspace/project/NPT-42 --experiment workspace/project/NPT-43
  
  # List unsynchronized experiments in the current directory without actually syncing
  python -m neptune.alpha.sync --list

  # List unsynchronized experiments in directory "foo/bar" without actually syncing
  python -m neptune.alpha.sync --list --location foo/bar

  # Synchronise all experiment in the current directory, sending offline experiments to project "workspace/project"
  python -m neptune.alpha.sync --project workspace/project

  # Synchronize only the offline experiment with UUID a1561719-b425-4000-a65a-b5efb044d6bb to project "workspace/project"
  python -m neptune.alpha.sync --project workspace/project --experiment a1561719-b425-4000-a65a-b5efb044d6bb
"""


parser = argparse.ArgumentParser(description='Synchronizes experiments with unsent data with the server',
                                 epilog=epilog, formatter_class=argparse.RawDescriptionHelpFormatter)
parser.add_argument('--list', action='store_true',
                    help='list unsynchronized experiments in the given directory without actually syncing')
parser.add_argument('-l', '--location', default='.', metavar='experiment-directory',
                    help="path to the directory containing a '.neptune' folder with stored experiments")
parser.add_argument('-e', '--experiment', action='append', metavar='experiment-name',
                    help="experiment name (workspace/project/short-id or UUID for offline experiments) to synchronize.")
parser.add_argument('-p', '--project', metavar='experiment-name',
                    help="project name (workspace/project) where offline experiments will be sent")


#######################################################################################################################
# Stubs of dynamically-generated classes for type checking and mocking in tests
#######################################################################################################################


@dataclass
class Experiment:
    uuid: str
    shortId: str
    organizationName: str
    projectName: str


#######################################################################################################################
# Experiment and Project utilities
#######################################################################################################################


# Set in the __main__ block, patched in tests
backend: NeptuneBackend = None


def report_get_experiment_error(experimentId: str, status_code: int, skipping: bool) -> None:
    comment = "Skipping experiment." if skipping else "Please try again later or contact Neptune team."
    print("Warning: Getting experiment {}: server responded with status code {}. {}"
          .format(experimentId, status_code, comment), file=sys.stderr)


def get_experiment(experiment_id: str) -> Optional[Experiment]:
    try:
        response = backend.leaderboard_client.api.getExperiment(experimentId=experiment_id).response()
        return response.result
    except HTTPError as e:
        if e.status_code in (401, 403, 404):
            report_get_experiment_error(experiment_id, e.status_code, skipping=True)
        else:
            report_get_experiment_error(experiment_id, e.status_code, skipping=False)


def get_project(project_name_flag: Optional[str]) -> Optional[Project]:
    project_name = project_name_flag or os.getenv(PROJECT_ENV_NAME)
    if not project_name:
        print('Project name not provided, so skipping synchronization of offline experiments.\n'
              'To synchronize offline experiment, specify the project name with the --project flag\n'
              'or by setting the {} environment variable.'.format(PROJECT_ENV_NAME), file=sys.stderr)
        return None
    try:
        return backend.get_project(project_name)
    except ProjectNotFound:
        print('Project {} not found, so skipping synchronization of offline experiments.\n'.format(project_name) +
              'Please ensure you specified the correct project name with the --project flag\n' +
              'or with the {} environment variable, or contact Neptune for support.\n'.format(PROJECT_ENV_NAME),
              file=sys.stderr)
        return None


def get_qualified_name(experiment: Experiment) -> str:
    return "{}/{}/{}".format(experiment.organizationName, experiment.projectName, experiment.shortId)


def is_valid_uuid(val: Any) -> bool:
    try:
        uuid.UUID(val)
        return True
    except ValueError:
        return False


#######################################################################################################################
# Listing experiments to be synchronized
#######################################################################################################################


def is_experiment_synced(experiment_path: Path) -> bool:
    sync_offset_file = SyncOffsetFile(experiment_path)
    sync_offset = sync_offset_file.read()
    if sync_offset is None:
        return False

    disk_queue = DiskQueue(str(experiment_path), OPERATIONS_DISK_QUEUE_PREFIX,
                           VersionedOperation.to_dict, VersionedOperation.from_dict)
    previous_operation = None
    while True:
        operation = disk_queue.get()
        if not operation:
            break
        previous_operation = operation
    if not previous_operation:
        return True

    return sync_offset >= previous_operation.version


def get_offline_experiments_ids(base_path: Path) -> List[str]:
    result = []
    for experiment_path in (base_path / OFFLINE_DIRECTORY).iterdir():
        if is_valid_uuid(experiment_path.name):
            result.append(experiment_path.name)
    return result


def partition_experiments(base_path: Path) -> Tuple[List[Experiment], List[Experiment]]:
    synced_experiment_uuids = []
    unsynced_experiment_uuids = []
    for experiment_path in base_path.iterdir():
        if is_valid_uuid(experiment_path.name):
            experiment_uuid = experiment_path.name
            if is_experiment_synced(experiment_path):
                synced_experiment_uuids.append(experiment_uuid)
            else:
                unsynced_experiment_uuids.append(experiment_uuid)
    synced_experiments = [experiment for experiment in map(get_experiment, synced_experiment_uuids) if experiment]
    unsynced_experiments = [experiment for experiment in map(get_experiment, unsynced_experiment_uuids) if experiment]
    return synced_experiments, unsynced_experiments


def list_experiments(path: Path, synced_experiments: Collection[Experiment],
                     unsynced_experiments: Collection[Experiment], offline_experiments_ids: Collection[str]) -> None:

    if not synced_experiments and not unsynced_experiments and not offline_experiments_ids:
        print('There are no Neptune experiments in ', path)
        sys.exit(1)

    if unsynced_experiments:
        print('Unsynchronized experiments:')
        for experiment in unsynced_experiments:
            print('-', get_qualified_name(experiment))

    if synced_experiments:
        print('Synchronized experiments:')
        for experiment in synced_experiments:
            print('-', get_qualified_name(experiment))

    if offline_experiments_ids:
        print('Unsynchronized offline experiments:')
        for experiment_id in offline_experiments_ids:
            print('-', experiment_id)
        print()
        print('Experiments which run offline are not created on the server, are not assigned to projects,\n'
              'and they are identified by UUIDs like the ones above instead of serial numbers.\n'
              'When synchronizing offline experiments, please specify the workspace and project using the "--project"\n'
              'flag. Alternatively, you can set the environment variable\n'
              '{} to the target workpspace/project. See the examples below.'.format(PROJECT_ENV_NAME))

    if not unsynced_experiments:
        print()
        print('There are no unsynchronized experiments in ', path)

    if not synced_experiments:
        print()
        print('There are no synchronized experiments in ', path)

    print()
    print('Please run with the --help flag to see example commands')


#######################################################################################################################
# Experiment synchronization
#######################################################################################################################


def sync_experiment(path: Path, qualified_experiment_name: str) -> None:
    experiment_uuid = uuid.UUID(path.name)
    print('Synchronising', qualified_experiment_name)

    disk_queue = DiskQueue(str(path), OPERATIONS_DISK_QUEUE_PREFIX,
                           VersionedOperation.to_dict, VersionedOperation.from_dict)
    sync_offset_file = SyncOffsetFile(path)
    sync_offset = sync_offset_file.read() or 0

    while True:
        batch = disk_queue.get_batch(1000)
        if not batch:
            print('Synchronization of experiment {} completed.'.format(qualified_experiment_name))
            return
        if batch[0].version > sync_offset:
            pass
        elif batch[-1].version <= sync_offset:
            continue
        else:
            for i, operation in enumerate(batch):
                if operation.version > sync_offset:
                    batch = batch[i:]
                    break
        backend.execute_operations(experiment_uuid, [op.op for op in batch])
        sync_offset_file.write(batch[-1].version)
        sync_offset = batch[-1].version


def sync_all_registered_experiments(path: Path) -> None:
    for experiment_path in path.iterdir():
        if is_valid_uuid(experiment_path.name) and not is_experiment_synced(experiment_path):
            experiment_uuid = experiment_path.name
            experiment = get_experiment(experiment_uuid)
            if experiment:
                sync_experiment(experiment_path, get_qualified_name(experiment))


def sync_selected_registered_experiments(path: Path, qualified_experiment_names: Collection[str]) -> None:
    for name in qualified_experiment_names:
        experiment = get_experiment(name)
        if experiment:
            experiment_path = path / experiment.uuid
            if experiment_path.exists():
                sync_experiment(experiment_path, name)
            else:
                print("Warning: Experiment '{}' does not exist in location {}".format(name, path), file=sys.stderr)


def register_offline_experiment(project: Project) -> Optional[Experiment]:
    try:
        experiment = backend.create_experiment(project.uuid)
        return Experiment(str(experiment.uuid), experiment.id, project.workspace, project.name)
    except Exception as e:
        print('Exception occurred while trying to create an experiment on the Neptune server. Please try again later',
              file=sys.stderr)
        logging.exception(e)
        return None


def move_offline_experiment(base_path: Path, offline_uuid: str, server_uuid: str) -> None:
    (base_path / OFFLINE_DIRECTORY / offline_uuid).rename(base_path / server_uuid)


def register_offline_experiments(base_path: Path, project: Project,
                                 offline_experiments_ids: Iterable[str]) -> List[Experiment]:
    result = []
    for experiment_uuid in offline_experiments_ids:
        experiment = register_offline_experiment(project)
        move_offline_experiment(base_path, experiment_uuid, experiment.uuid)
        print('Offline experiment {} registered as {}'.format(experiment_uuid, get_qualified_name(experiment)))
        result.append(experiment)
    return result


def references_offline_experiment(base_path: Path, name: str) -> bool:
    return is_valid_uuid(name) and (base_path / OFFLINE_DIRECTORY / name).is_dir()


def sync_selected_experiments(base_path: Path, project_name: Optional[str],
                              experiment_names: Collection[str]) -> None:
    f = partial(references_offline_experiment, base_path)
    offline_experiment_ids = list(filter(f, experiment_names))
    other_experiment_names = list(filterfalse(f, experiment_names))
    if offline_experiment_ids:
        project = get_project(project_name)
        if project:
            registered_experiments = register_offline_experiments(base_path, project, offline_experiment_ids)
            other_experiment_names.extend(get_qualified_name(exp) for exp in registered_experiments)
    sync_selected_registered_experiments(base_path, other_experiment_names)


def sync_all_experiments(base_path: Path, project_name: Optional[str]) -> None:
    offline_experiment_ids = get_offline_experiments_ids(base_path)
    if offline_experiment_ids:
        project = get_project(project_name)
        if project:
            register_offline_experiments(base_path, project, offline_experiment_ids)
    sync_all_registered_experiments(base_path)


#######################################################################################################################
# Entrypoint for the CLI utility
#######################################################################################################################


def main():
    args = parser.parse_args()

    # get base path
    if args.location:
        path = Path.cwd() / Path(args.location)
    else:
        path = Path.cwd()

    # check if path exists and contains a '.neptune' folder
    if (path / NEPTUNE_EXPERIMENT_DIRECTORY).is_dir():
        path = path / NEPTUNE_EXPERIMENT_DIRECTORY
    elif path.name == NEPTUNE_EXPERIMENT_DIRECTORY and path.is_dir():
        pass
    else:
        error_message = \
            ("Path {} does not contain a '{}' folder. Please specify a path to a folder with Neptune experiments."
             .format(path, NEPTUNE_EXPERIMENT_DIRECTORY))
        print(error_message, file=sys.stderr)
        sys.exit(1)

    experiment_names = args.experiment

    if args.list:
        synced_experiments, unsynced_experiments = partition_experiments(path)
        offline_experiments_ids = get_offline_experiments_ids(path)
        list_experiments(path, synced_experiments, unsynced_experiments, offline_experiments_ids)
    elif experiment_names:
        sync_selected_experiments(path, args.project, experiment_names)
    else:
        sync_all_experiments(path, args.project)


if __name__ == '__main__':
    backend = HostedNeptuneBackend(Credentials())
    main()
