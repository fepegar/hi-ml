#  ------------------------------------------------------------------------------------------
#  Copyright (c) Microsoft Corporation. All rights reserved.
#  Licensed under the MIT License (MIT). See LICENSE in the repo root for license information.
#  ------------------------------------------------------------------------------------------
import logging
import os
from pathlib import Path
from typing import List, Optional

import torch.multiprocessing
from pytorch_lightning import LightningModule, seed_everything

from health_azure import AzureRunInfo
from health_azure.utils import (create_run_recovery_id, ENV_OMPI_COMM_WORLD_RANK,
                                is_running_in_azure_ml, PARENT_RUN_CONTEXT, RUN_CONTEXT)

from health_ml.experiment_config import ExperimentConfig
from health_ml.lightning_container import LightningContainer
from health_ml.model_trainer import create_lightning_trainer, model_train
from health_ml.utils import fixed_paths
from health_ml.utils.checkpoint_utils import CheckpointHandler
from health_ml.utils.common_utils import (
    EFFECTIVE_RANDOM_SEED_KEY_NAME, logging_section,
    RUN_RECOVERY_ID_KEY, RUN_RECOVERY_FROM_ID_KEY_NAME)
from health_ml.utils.lightning_loggers import StoringLogger
from health_ml.utils.type_annotations import PathOrString


def check_dataset_folder_exists(local_dataset: PathOrString) -> Path:
    """
    Checks if a folder with a local dataset exists. If it does exist, return the argument converted
    to a Path instance. If it does not exist, raise a FileNotFoundError.

    :param local_dataset: The dataset folder to check.
    :return: The local_dataset argument, converted to a Path.
    """
    expected_dir = Path(local_dataset)
    if not expected_dir.is_dir():
        raise FileNotFoundError(f"The model uses a dataset in {expected_dir}, but that does not exist.")
    logging.info(f"Model training will use the local dataset provided in {expected_dir}")
    return expected_dir


class MLRunner:

    def __init__(self,
                 experiment_config: ExperimentConfig,
                 container: LightningContainer,
                 project_root: Optional[Path] = None) -> None:
        """
        Driver class to run a ML experiment. Note that the project root argument MUST be supplied when using hi-ml
        as a package!

        :param container: The LightningContainer object to use for training.
        :param project_root: Project root. This should only be omitted if calling run_ml from the test suite. Supplying
        it is crucial when using hi-ml as a package or submodule!
        """
        self.container = container
        self.experiment_config = experiment_config
        self.container.num_nodes = self.experiment_config.num_nodes
        self.project_root: Path = project_root or fixed_paths.repository_root_directory()
        self.storing_logger: Optional[StoringLogger] = None
        self._has_setup_run = False

    def setup(self, azure_run_info: Optional[AzureRunInfo] = None) -> None:
        """
        Sets the random seeds, calls the setup method on the LightningContainer and then creates the actual
        Lightning modules.

        :param azure_run_info: When running in AzureML or on a local VM, this contains the paths to the datasets.
        This can be missing when running in unit tests, where the local dataset paths are already populated.
        """
        if self._has_setup_run:
            return
        if azure_run_info:
            # Set up the paths to the datasets. azure_run_info already has all necessary information, using either
            # the provided local datasets for VM runs, or the AzureML mount points when running in AML.
            # This must happen before container setup because that could already read datasets.
            if len(azure_run_info.input_datasets) > 0:
                input_datasets = azure_run_info.input_datasets
                assert len(input_datasets) > 0
                local_datasets = [
                    check_dataset_folder_exists(input_dataset) for input_dataset in input_datasets  # type: ignore
                ]
                self.container.local_datasets = local_datasets  # type: ignore
        # Ensure that we use fixed seeds before initializing the PyTorch models
        seed_everything(self.container.get_effective_random_seed())

        # Creating the folder structure must happen before the LightningModule is created, because the output
        # parameters of the container will be copied into the module.
        self.container.create_filesystem(self.project_root)

        # configure recovery container if provided
        self.checkpoint_handler = CheckpointHandler(container=self.container,
                                                    project_root=self.project_root,
                                                    run_context=RUN_CONTEXT)
        self.checkpoint_handler.download_recovery_checkpoints_or_weights()  # type: ignore

        self.container.setup()
        self.container.create_lightning_module_and_store()
        self._has_setup_run = True

    def set_run_tags_from_parent(self) -> None:
        """
        Set metadata for the run
        """
        assert PARENT_RUN_CONTEXT, "This function should only be called in a Hyperdrive run."
        run_tags_parent = PARENT_RUN_CONTEXT.get_tags()
        tags_to_copy = [
            "tag",
            "model_name",
            "execution_mode",
            "recovered_from",
            "friendly_name",
            "build_number",
            "build_user",
            RUN_RECOVERY_FROM_ID_KEY_NAME
        ]
        new_tags = {tag: run_tags_parent.get(tag, "") for tag in tags_to_copy}
        new_tags[RUN_RECOVERY_ID_KEY] = create_run_recovery_id(run=RUN_CONTEXT)
        new_tags[EFFECTIVE_RANDOM_SEED_KEY_NAME] = str(self.container.get_effective_random_seed())
        RUN_CONTEXT.set_tags(new_tags)

    def run(self) -> None:
        """
        Driver function to run a ML experiment
        """
        self.setup()
        is_offline_run = not is_running_in_azure_ml(RUN_CONTEXT)
        # Get the AzureML context in which the script is running
        if not is_offline_run and PARENT_RUN_CONTEXT is not None:
            logging.info("Setting tags from parent run.")
            self.set_run_tags_from_parent()

        # do training
        with logging_section("Model training"):
            if self.checkpoint_handler is not None:
                checkpoint_path = self.checkpoint_handler.get_recovery_or_checkpoint_path_train()
            else:
                checkpoint_path = None
            _, storing_logger = model_train(checkpoint_path,
                                            container=self.container)
            self.storing_logger = storing_logger

        # Since we have trained the model, let the checkpoint_handler object know so it can handle
        # checkpoints correctly.
        if self.checkpoint_handler is not None:
            self.checkpoint_handler.additional_training_done()
            checkpoint_paths_for_testing = self.checkpoint_handler.get_checkpoints_to_test()
        else:
            checkpoint_paths_for_testing = []

        with logging_section("Model inference"):
            self.run_inference(checkpoint_paths_for_testing)

    def run_inference(self, checkpoint_paths: List[Path]) -> None:
        """
        Run inference on the test set for all models.

        :param checkpoint_paths: The path to the checkpoint that should be used for inference.
        """
        if len(checkpoint_paths) != 1:
            raise ValueError(f"This method expects exactly 1 checkpoint for inference, but got {len(checkpoint_paths)}")

        lightning_model = self.container.model
        if type(lightning_model).test_step != LightningModule.test_step:
            # Run Lightning's built-in test procedure if the `test_step` method has been overridden
            logging.info("Running inference via the LightningModule.test_step method")
            # Lightning does not cope with having two calls to .fit or .test in the same script. As a workaround for
            # now, restrict number of GPUs to 1, meaning that it will not start DDP.
            self.container.max_num_gpus = 1
            # Without this, the trainer will think it should still operate in multi-node mode, and wrongly start
            # searching for Horovod
            if ENV_OMPI_COMM_WORLD_RANK in os.environ:
                del os.environ[ENV_OMPI_COMM_WORLD_RANK]
            # From the training setup, torch still thinks that it should run in a distributed manner,
            # and would block on some GPU operations. Hence, clean up distributed training.
            if torch.distributed.is_initialized():  # type: ignore
                torch.distributed.destroy_process_group()  # type: ignore

            trainer, _ = create_lightning_trainer(self.container, num_nodes=1)

            self.container.load_model_checkpoint(checkpoint_path=checkpoint_paths[0])
            # Change the current working directory to ensure that test files go to thr right folder
            data_module = self.container.get_data_module()

            _ = trainer.test(self.container.model, datamodule=data_module)

        else:
            logging.warning("None of the suitable test methods is overridden. Skipping inference completely.")