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

"""
# Running Cluster GCN Step by Step
"""

"""
In this tutorial we show how to train the Cluster-GCN algorithm,
presented in [Cluster-GCN: An Efficient Algorithm for Training Deep and Large Graph Convolutional
Networks](https://arxiv.org/pdf/1905.07953.pdf), on the Graphcore IPU.

In this tutorial, you will learn how to train and test the model step by step, including:

- Load the data.
- Configure the IPU.
- Create a strategy.
- Create the model.
- Train and test the model.

[1] Wei-Lin Chiang, Xuanqing Liu, Si Si, Yang Li, Samy Bengio, Cho-Jui Hsieh,
"Cluster-GCN An Efficient Algorithm for Training Deep and Large Graph Convolutional Networks,"
KDD '19: Proceedings of the 25th ACM SIGKDD International Conference on Knowledge Discovery &
Data Mining, July 2019, Pages 257–266, https://doi.org/10.1145/3292500.3330925
"""

"""
## Introduction

Cluster GCN can be considered a sampling method that allows to train on large scale graph machine learning datasets. Cluster GCN works on two steps. First, it clusters the data, such that each cluster is a subgraph. Second, the algorithm trains the model using a stochastic gradient estimated with a batch of sampled subgraphs.

By default, we will train with the [arXiv dataset](https://ogb.stanford.edu/docs/nodeprop/#ogbn-arxiv), which is a directed homogeneous graph encoding a citation network between all Computer Science (CS) papers hosted in arXiv and indexed by the Microsoft academic graph (MAG). Each paper has a 128-dimmensional node feature vector, that encodes the title and abstract, processed with a skip-gram model. Each directed link in the graph indicates that one paper cites another. The task is to predict the correct topic label for the paper from the 40 main categories. The train portion of the dataset is all papers published until 2017, the papers published in 2018 are the validation set, and papers published in 2019 are the test set. We wukk use the arXiv dataset, simply use the train_arxiv.json config, the dataset will be downloaded automatically.

We will also show how to train with other common datasets.
"""

"""
## Preliminaries
"""
# !apt-get update
# !apt-get install -y libmetis-dev=5.1.0.dfsg-5
# %pip install -r requirements.txt

"""
"""

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

"""
## Configuration
"""

"""
Setup logging
"""
logging.getLogger().setLevel("INFO")
logging.basicConfig(format="%(asctime)s %(levelname)-8s %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
# Prevent doubling of TF logs.
tf.get_logger().propagate = False

"""
Define config file and use default values for those not defined in the config file.
By default, we will use a config file for the arXiv dataset.

But we encourage you to experiment with the other example files we provide too:

- configs/train_arxiv.json
- configs/train_mag.json
- configs/train_mag240.json
- configs/train_ppi.json
- configs/train_products.json
- configs/train_reddit.json

Note that the PPI and Reddit datasets have to be downloaded manually from [Stanford GraphSAGE](https://snap.stanford.edu/graphsage/). The rest of datasets will download automatically. Note also some datasets, like Products and MAG, can take long to download, preprocess and clustering. The MAG240 dataset can take several hours for this preprocessing.  Moreover, note that the clustering method used in the original paper [1] -and also the one implemented here- has been designed for homogeneous graphs. However, two of the config files described above, namely `configs/train_mag.json` and `configs/train_mag240.json` that correspond with MAG and MAG240 datasets, respectively, include heterogeneous graphs. Hence, clustering these datasets as if they were homogeneous graphs can reduce the accuracy.
"""
config_file = "configs/train_arxiv.json"

"""
"""
with open(config_file, "r") as read_file:
    config = json.load(read_file)
config = Options(**config)

logging.info(f"Config: {pformat(config.dict())}")


"""
We disable logging results in Weights and Biases by overwriting the `config.wandb` parameter from the config file and set it to false.
"""
config.wandb = False

"""
### Training configuration

Set training precision and the way we represent the adjacency matrix from the config file. For the model precision we can choose FP32 or FP16; while for the representation of the adjacency matrix, we can choose either dense or the COO sparse representation.

You can also override the precision configuration in the code. For example, in order to set FP16 and COO representation just do the following:
```
config.training.precision = "fp16"
config.training.use_sparse_representation = True
```
"""
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


"""
Set a unique name for this training run. This is useful when logging data, especially when logging the results on Weights and Biases.
"""
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


"""
## Load the dataset

We are now ready to load the dataset. We only have to introduce the path to the dataset. This path will be used to look for available preprocessed data and cached clustering results.
"""
config.data_path = "graph_datasets/"

"""
"""
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


"""
## Training

### Clustering the training dataset

Once we have loaded the dataset, we can cluster the associated graph, using the parameters given in the config file, mainly `config.training.num_clusters` or `config.training.max_nodes_per_batch`. The former sets the total number of clusters in which we want to split the graph. The latter specifies the maximum number of nodes per cluster. Note these are alternative methods, so we cannot specify both parameters.

Other important parameters are:
- `config.training.clusters_per_batch`: Number of clusters to be processed together in a micro-batch.

- `method_max_nodes` and `method_max_edges`: In order to maximise bandwidth, we compile the model as a static graph. This requires having static datastructures. Since the number of nodes can change from one cluster to another, we need a method to set the maximum number of nodes per cluster. If a cluster has a number of nodes smaller than what we have defined, then it will pad with a dummy node so that it fits the allocated memory, wasting computations and reducing throughput. On the other hand, if they are larger, it will remove some of them so that they fit in the allocated space, removing data and potentially reducing accuracy. Though there is a tradeoff here, and we offer three methods to optimise this tradeoff
namely `average`, `average_plus_std` and `upper_bound`, which work as follows:
    - `average` means all clusters will pad ro prune nodes so that they adjust to
the average number of nodes across all clusters;
    - `average_plus_std` means all clusters will adjust to the average number of nodes across all clusters
plus one standard deviation;
    - `upper_bound` means all clusters will pad to the number of nodes in the largest cluster.
In practice, the gains obtained by using a statically compiled graph are larger than the extra computations due to padding, even when using the `upper_bound` case.

- `method_max_edges`: Same as `method_max_nodes` but for the number of edges per cluster.

- `config.inter_cluster_ratio`: When using COO representation and sampling multiple cluster per batch, this parameter represents the extra number of edges that connect nodes that are in different that we will use. When this is zero, we can think the adjacency matrix of the batch as a pure block-diagonal matrix. If this value is greater than zero, then the adjacency matrix will have some positive values off the diagonal blocks.

- `node_edge_imbalance_ratio`: By default the METIS clustering algorithm will find clusters that have similar number of nodes, though they can have very different number of edges. Setting this parameter we can tell METIS to find a trade-off between balancing number of nodes and edges. In theory this could reduce the amount of padding when using `upper_bound` method to set number of nodes and edges, but in practice we have observed but it doesn't have much effect on throughput for the considered datasets.

We encourage you playing with different number of clusters and clusters per batch. The idea is to keep the number of nodes per batch large enough to minimise the number of lost edges, which affects accuracy, while fitting in memory, so we can benefit from the high bandwidth offered by a compiled computational graph.
"""

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

"""
We can compute some statistics on the impact of clustering on the graph
"""
clustering_statistics = ClusteringStatistics(
    training_clusters.adjacency,
    training_clusters.clusters,
    num_clusters_per_batch=config.training.clusters_per_batch)
clustering_statistics.get_statistics(wandb=config.wandb)

"""
Count the number of nodes that will be processed per epoch.
"""
num_real_nodes_per_epoch = len(dataset.dataset_splits["train"])

"""
### Build training dataset generator

Create a efficient dataset generator for training using the TensorFlow `tf.data.Dataset` API. This allows preprocessing the data with multiple threads, increasing the speed at which the host can feed data to the Graphcore IPU. This is important, as the Graphcore IPU is so fast for GNN, that the host is usually the bottleneck!
"""
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

"""
As mentioned above, the Graphcore Bow-IPU is so fast that the dataset generator is usually the bottleneck, especially when we use distributed training with data parallelism, in which multiple replicas of the model run in different Graphcore IPU devices of a Graphcore Bow-POD.

In order to be able to use the high throughput provided by a Graphcore Bow-POD-4, we offer the `poprun` tool, that allows to launch multiple instances in the host, each one feeding data to one of the replicas. This is really easy to do from the command line, as explained in the README.md of this repo.

The next cell gives some feedback about this.
"""
if config.training.replicas > 1:
    logging.warning(f"Increasing the number of model replicas will scale up the throughput if "
                    f"the data generator in the host side is fast enough to feed data to all "
                    f"the replicas. This is easily achieved with `poprun`. See README.md from "
                    f"this repo for more information on how to scale the host throughput by "
                    f"running multiple instances in the host, feeding one replica each.")

"""
Create a batch config object that calculates the number of steps, micro-batches, etc. This is useful for distributed training, so we don't have to keep the numbers in mind when allocating data to the different replicas and/or pipeline stages.
"""
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

"""
### Training strategy

We are ready to create the model. In order to compile the model for different hardware, we leverage TensorFlow's strategy scope, so that we can define the same Keras model to run on a POD of IPUs or on a CPU, and everything is handled under the hood, transparently for the user.
"""
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

"""
### Creating and compile model, and training loop

Create, compile and train the model within the desired strategy scope.
"""
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

"""
## Test the trained model

Once we have trained the model, let's evaluate how it performs on the test dataset.
"""

"""
### Test configuration

Note we usually have different requirements for testing than training. For example, while we may want to train the model with FP16 arithmetic for reduced memory and increased throughput, we typically want to test the model with FP32 to avoid loosing accuracy.

Here, again, we use the values from the config file.
"""
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

"""
### Cluster test dataset

Since the test dataset is different from the training dataset, it has also to be clustered independently.

In practice, we want to evaluate the performance of the trained model on the full test graph, so no artefacts are introduced while measuring the accuracy. Note that evaluation only requires a forward pass per batch, which is computationally much less demanding that the multiple forward and backward passes per batch required during training. Hence, we usually can afford testing the model on the CPU, which though slower, usually has lots of memory available.

In the case the graph dataset is too large so that it doesn't fit even on CPU memory, we just have to cluster the test dataset.

In any case, we use the same `ClusterGraph` class that we used for the training set, since it is convenient to compute and store the maximum number of nodes and edges per batch.
"""
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

"""
## Test dataset generator

Create an efficient dataset generator that can feed the Keras Model.evaluate method.
"""
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

"""
Since there is only one cluster or subgraph (the whole graph) and there is no distributed training, we do not need to create a batch config object when testing on the CPU.

We do not have to create a strategy when testing on the CPU either, since this is the default device for TensorFlow.
"""

"""
## Model

Create the model for test that will run on the CPU.
"""
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

"""
Copy the weights from training.
"""
model_test.set_weights(trained_weights)

"""
Get the loss function and other metrics.
"""
_, accuracy, f1_score_macro, f1_score_micro = get_loss_and_metrics(
    task=dataset.task,
    num_labels=dataset.num_labels,
    adjacency_form=adjacency_form_test,
    metrics_precision=precision.metrics_precision,
    enable_loss_outfeed=False)

"""
Compile the model.
"""
model_test.compile(metrics=[accuracy, f1_score_macro, f1_score_micro],
                   steps_per_execution=1)

"""
Run test on the test dataset.
"""
results = model_test.evaluate(data_generator_test,
                              steps=1)

logging.info(f"Test Accuracy: {results[1]},"
             f" Test F1 macro: {results[2]},"
             f" Test F1 micro: {results[3]}")
logging.info("Test complete")
