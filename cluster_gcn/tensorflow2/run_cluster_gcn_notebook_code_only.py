# Copyright (c) 2022 Graphcore Ltd. All rights reserved.
# Copyright 2022 The Google Research Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# THIS FILE IS AUTOGENERATED. Rerun SST after editing source file: run_cluster_gcn_notebook.py

# !apt-get update
# !apt-get install -y libmetis-dev=5.1.0.dfsg-5
%pip install - r requirements.txt

from datetime import datetime
import json
import logging
from pprint import pformat

import numpy as np
import popdist.tensorflow
import tensorflow as tf

from data_utils.batch_config import BatchConfig
from data_utils.clustering_utils import ClusterGraph
from data_utils.clustering_statistics import ClusteringStatistics
from data_utils.dataset_batch_generator import tf_dataset_generator
from data_utils.dataset_loader import load_dataset
from keras_extensions.callbacks.callback_factory import CallbackFactory
from keras_extensions.optimization import get_optimizer
from model.loss_accuracy import get_loss_and_metrics
from model.model import create_model
from model.pipeline_stage_names import (
    PIPELINE_ALLOCATE_PREVIOUS,
    PIPELINE_NAMES
)
from model.precision import Precision
from utilities.constants import GraphType
from utilities.ipu_utils import create_ipu_strategy, set_random_seeds
from utilities.options import Options
from utilities.pipeline_stage_assignment import pipeline_model
from utilities.utils import (
    get_adjacency_dtype,
    get_adjacency_form,
    get_method_max
)

logging.getLogger().setLevel("INFO")
logging.basicConfig(format="%(asctime)s %(levelname)-8s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
# Prevent doubling of TF logs.
tf.get_logger().propagate = False

config_file = "configs/train_arxiv.json"

with open(config_file, "r") as read_file:
    config = json.load(read_file)
config = Options(**config)

logging.info(f"Config: {pformat(config.dict())}")

config.wandb = False

# Set precision policy for training
precision = Precision(config.training.precision)
tf.keras.mixed_precision.set_global_policy(precision.policy)

# Set how the adjacency matrix is expressed,
# namely dense (tf.Tensor), dynamic COO representation (tf.sparse.SparseTensor),
# or static COO representation with padding (tuple).
adjacency_form_training = get_adjacency_form(
    config.training.device,
    config.training.use_sparse_representation)

# Decide on the dtype of the adjacency matrix
adjacency_dtype_training = get_adjacency_dtype(
    config.training.device,
    config.training.use_sparse_representation)

method_max_edges = get_method_max(config.method_max_edges)
method_max_nodes = get_method_max(config.method_max_nodes)

time_now = datetime.now().timestamp()
universal_run_name = (
    f"{config.name}-"
    f"{config.dataset_name}-"
    f"{config.training.precision}-"
    f"{config.training.device}-"
    f"{adjacency_form_training}-"
    f"{datetime.fromtimestamp(time_now).strftime('%Y%m%d_%H%M%S')}"
)
logging.info(f"Universal name for run: {universal_run_name}")

config.data_path = "/localdata/paperspace/graph_datasets/"

dataset = load_dataset(
    dataset_path=config.data_path,
    dataset_name=config.dataset_name,
    precalculate_first_layer=config.model.first_layer_precalculation,
    adjacency_dtype=adjacency_dtype_training,
    features_dtype=precision.features_precision.as_numpy_dtype,
    labels_dtype=precision.labels_precision.as_numpy_dtype,
    regenerate_cache=config.regenerate_dataset_cache,
    save_dataset_cache=config.save_dataset_cache,
    pca_features_path=config.pca_features_path,
)
logging.info(f"Graph dataset: {dataset}")

training_clusters = ClusterGraph(
    adjacency=dataset.adjacency_train,
    num_clusters=config.training.num_clusters,
    visible_nodes=dataset.dataset_splits["train"],
    max_nodes_per_batch=config.training.max_nodes_per_batch,
    clusters_per_batch=config.training.clusters_per_batch,
    dataset_name=config.dataset_name + "-training",
    cache_dir=config.data_path,
    regenerate_cluster_cache=config.regenerate_clustering_cache,
    save_clustering_cache=config.save_clustering_cache,
    directed_graph=(dataset.graph_type == GraphType.DIRECTED),
    adjacency_form=adjacency_form_training,
    inter_cluster_ratio=config.inter_cluster_ratio,
    method_max_nodes=method_max_nodes,
    method_max_edges=method_max_edges,
    node_edge_imbalance_ratio=config.cluster_node_edge_imbalance_ratio
)
training_clusters.cluster_graph()

clustering_statistics = ClusteringStatistics(
    training_clusters.adjacency,
    training_clusters.clusters,
    num_clusters_per_batch=config.training.clusters_per_batch)
clustering_statistics.get_statistics(wandb=config.wandb)

num_real_nodes_per_epoch = len(dataset.dataset_splits["train"])

data_generator_training = tf_dataset_generator(
    adjacency=dataset.adjacency_train,
    clusters=training_clusters.clusters,
    features=dataset.features_train,
    labels=dataset.labels,
    mask=dataset.mask_train,
    num_clusters=training_clusters.num_clusters,
    clusters_per_batch=training_clusters.clusters_per_batch,
    max_nodes_per_batch=training_clusters.max_nodes_per_batch,
    max_edges_per_batch=training_clusters.max_edges_per_batch,
    adjacency_dtype=adjacency_dtype_training,
    adjacency_form=adjacency_form_training,
    micro_batch_size=config.training.micro_batch_size,
    seed=config.seed,
    prefetch_depth=config.training.dataset_prefetch_depth,
    distributed_worker_count=popdist.getNumInstances(),
    distributed_worker_index=popdist.getInstanceIndex()
)
logging.info(
    f"Created batch generator for training: {data_generator_training}")

if config.training.replicas > 1:
    logging.warning(f"Increasing the number of model replicas will scale up the throughput if "
                    f"the data generator in the host side is fast enough to feed data to all "
                    f"the replicas. This is easily achieved with `poprun`. See README.md from "
                    f"this repo for more information on how to scale the host throughput by "
                    f"running multiple instances in the host, feeding one replica each.")

num_real_nodes_per_epoch = len(dataset.dataset_splits["train"])
batch_config_training = BatchConfig(
    micro_batch_size=config.training.micro_batch_size,
    num_clusters=training_clusters.num_clusters,
    clusters_per_batch=training_clusters.clusters_per_batch,
    max_nodes_per_batch=training_clusters.max_nodes_per_batch,
    executions_per_epoch=config.training.executions_per_epoch,
    gradient_accumulation_steps_per_replica=config.training.gradient_accumulation_steps_per_replica,
    num_replicas=config.training.replicas,
    epochs_per_execution=config.training.epochs_per_execution,
    num_real_nodes_per_epoch=num_real_nodes_per_epoch,
    num_epochs=config.training.epochs)
logging.info(f"Training batch config:\n{batch_config_training}")

# Calculate the number of pipeline stages and the number of required IPUs per replica.
num_pipeline_stages_training = len(
    config.training.ipu_config.pipeline_device_mapping)
num_ipus_per_replica_training = max(
    config.training.ipu_config.pipeline_device_mapping) + 1

# Create a strategy scope for training
strategy_training_scope = create_ipu_strategy(
    num_ipus_per_replica=num_pipeline_stages_training,
    num_replicas=config.training.replicas,
    matmul_available_memory_proportion=config.training.ipu_config.matmul_available_memory_proportion_per_pipeline_stage[0],
    matmul_partials_type=precision.matmul_partials_type,
    compile_only=config.compile_only,
    enable_recomputation=config.training.ipu_config.enable_recomputation,
    fp_exceptions=config.fp_exceptions,
    num_io_tiles=config.training.ipu_config.num_io_tiles
).scope() if config.training.device == "ipu" else tf.device("/cpu:0")

# Seed the random generators for reproducibility
set_random_seeds(config.seed)

with strategy_training_scope:
    # Create the model for training
    model_training = create_model(
        micro_batch_size=config.training.micro_batch_size,
        num_labels=dataset.num_labels,
        num_features=dataset.num_features,
        max_nodes_per_batch=training_clusters.max_nodes_per_batch,
        max_edges_per_batch=training_clusters.max_edges_per_batch,
        hidden_size=config.model.hidden_size,
        num_layers=config.model.num_layers,
        dropout_rate=config.model.dropout,
        adjacency_params=config.model.adjacency.dict(),
        cast_model_inputs_to_dtype=precision.cast_model_inputs_to_dtype,
        first_layer_precalculation=config.model.first_layer_precalculation,
        use_ipu_layers=(config.training.device == "ipu"),
        adjacency_form=adjacency_form_training
    )
    model_training.summary(print_fn=logging.info)

    # Set options for the infeed and outfeed buffers that connect IPU and host.
    model_training.set_infeed_queue_options(prefetch_depth=10)
    model_training.set_outfeed_queue_options(buffer_depth=10)

    if num_pipeline_stages_training > 1 and config.training.device == "ipu":
        # Pipeline the model if required
        pipeline_model(model=model_training,
                       config=config.training,
                       pipeline_names=PIPELINE_NAMES,
                       pipeline_allocate_previous=PIPELINE_ALLOCATE_PREVIOUS,
                       num_ipus_per_replica=num_ipus_per_replica_training,
                       matmul_partials_type=precision.matmul_partials_type)
    elif config.training.gradient_accumulation_steps_per_replica > 1:
        # Set gradient accumulation if requested. If the model is pipelined
        # this is done through the pipeline API above.
        model_training.set_gradient_accumulation_options(
            gradient_accumulation_steps_per_replica=
            config.training.gradient_accumulation_steps_per_replica
        )

    # Build the loss function and other metrics
    loss, accuracy, f1_score_macro, f1_score_micro = get_loss_and_metrics(
        task=dataset.task,
        num_labels=dataset.num_labels,
        adjacency_form=adjacency_form_training,
        metrics_precision=precision.metrics_precision,
        enable_loss_outfeed=(config.training.device == "ipu"))

    # Build the optimizer
    optimizer = get_optimizer(
        gradient_accumulation_steps_per_replica=config.training.gradient_accumulation_steps_per_replica,
        num_replicas=config.training.replicas,
        learning_rate=tf.cast(config.training.lr, dtype=tf.float32),
        loss_scaling=config.training.loss_scaling,
        optimizer_compute_precision=precision.optimizer_compute_precision
    )

    # Compile the model
    model_training.compile(
        optimizer=optimizer,
        loss=loss,
        metrics=[accuracy, f1_score_macro, f1_score_micro],
        steps_per_execution=batch_config_training.steps_per_execution,
    )

    # Create any callbacks required for training
    callbacks_training = CallbackFactory.get_callbacks(
        universal_run_name=universal_run_name,
        num_nodes_processed_per_execution=batch_config_training.num_nodes_processed_per_execution,
        real_over_padded_ratio=batch_config_training.real_over_padded_ratio,
        total_num_epochs=batch_config_training.scaled_num_epochs,
        checkpoint_path=config.save_ckpt_path.joinpath(universal_run_name),
        config=config.dict(),
        executions_per_log=config.executions_per_log,
        executions_per_ckpt=config.executions_per_ckpt,
        outfeed_queues=[loss.outfeed_queue]
    )

    # Train the model
    model_training.fit(
        data_generator_training,
        epochs=batch_config_training.scaled_num_epochs,
        steps_per_epoch=batch_config_training.steps_per_epoch,
        callbacks=callbacks_training,
        verbose=0
    )

trained_weights = model_training.get_weights()
logging.info("Training complete")

# Set the precision policy for testing
precision = Precision(config.test.precision)
tf.keras.mixed_precision.set_global_policy(precision.policy)

# Set how the adjacency matrix is expressed,
# namely dense tensor, sparse tensor, or tuple.
adjacency_form_test = get_adjacency_form(
    config.test.device,
    config.test.use_sparse_representation)
# Decide on the dtype of the adjacency matrix
adjacency_dtype_test = get_adjacency_dtype(
    config.test.device,
    config.test.use_sparse_representation)

# Cluster the test graph
test_clusters = ClusterGraph(
    adjacency=dataset.adjacency_full,
    num_clusters=config.test.num_clusters,
    visible_nodes=np.arange(dataset.total_num_nodes),
    max_nodes_per_batch=config.test.max_nodes_per_batch,
    clusters_per_batch=config.test.clusters_per_batch,
    dataset_name=config.dataset_name + "-test",
    cache_dir=config.data_path,
    regenerate_cluster_cache=config.regenerate_clustering_cache,
    save_clustering_cache=config.save_clustering_cache,
    directed_graph=(dataset.graph_type == GraphType.DIRECTED),
    adjacency_form=adjacency_form_test,
    inter_cluster_ratio=config.inter_cluster_ratio,
    method_max_nodes=method_max_nodes,
    method_max_edges=method_max_edges,
    node_edge_imbalance_ratio=config.cluster_node_edge_imbalance_ratio
)
test_clusters.cluster_graph()

data_generator_test = tf_dataset_generator(
    adjacency=dataset.adjacency_full,
    clusters=test_clusters.clusters,
    features=dataset.features,
    labels=dataset.labels,
    mask=dataset.mask_test,
    num_clusters=config.test.num_clusters,
    clusters_per_batch=config.test.clusters_per_batch,
    max_nodes_per_batch=test_clusters.max_nodes_per_batch,
    max_edges_per_batch=test_clusters.max_edges_per_batch,
    adjacency_dtype=adjacency_dtype_test,
    adjacency_form=adjacency_form_test,
    micro_batch_size=config.test.micro_batch_size,
    seed=config.seed
)
logging.info(
    f"Created batch generator for test: {data_generator_test}")

set_random_seeds(config.seed + 1)

model_test = create_model(
    micro_batch_size=config.test.micro_batch_size,
    num_labels=dataset.num_labels,
    num_features=dataset.num_features,
    max_nodes_per_batch=test_clusters.max_nodes_per_batch,
    max_edges_per_batch=test_clusters.max_edges_per_batch,
    hidden_size=config.model.hidden_size,
    num_layers=config.model.num_layers,
    dropout_rate=config.model.dropout,
    adjacency_params=config.model.adjacency.dict(),
    cast_model_inputs_to_dtype=precision.cast_model_inputs_to_dtype,
    first_layer_precalculation=config.model.first_layer_precalculation,
    use_ipu_layers=(config.test.device == "ipu"),
    adjacency_form=adjacency_form_test
)

model_test.set_weights(trained_weights)

_, accuracy, f1_score_macro, f1_score_micro = get_loss_and_metrics(
    task=dataset.task,
    num_labels=dataset.num_labels,
    adjacency_form=adjacency_form_test,
    metrics_precision=precision.metrics_precision,
    enable_loss_outfeed=False)

model_test.compile(metrics=[accuracy, f1_score_macro, f1_score_micro],
                   steps_per_execution=1)

results = model_test.evaluate(data_generator_test,
                              steps=1)

logging.info(f"Test Accuracy: {results[1]},"
             f" Test F1 macro: {results[2]},"
             f" Test F1 micro: {results[3]}")
logging.info("Test complete")

# Generated:2022-07-21T19:14 Source:run_cluster_gcn_notebook.py SST:0.0.7
