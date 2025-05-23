from mnist import download_and_parse_mnist_file
import numpy as np
from collections import namedtuple

from ml_genn import Connection, Network, Population
from ml_genn.callbacks import SpikeRecorder, Callback, Checkpoint, VarRecorder
from ml_genn.compilers import EventPropCompiler
from ml_genn.connectivity import Dense, FixedProbability
from ml_genn.initializers import Normal, Uniform
from ml_genn.neurons import LeakyIntegrate, LeakyIntegrateFire, SpikeInput
from ml_genn.optimisers import Adam
from ml_genn.serialisers import Numpy
from ml_genn.synapses import Exponential


from ml_genn.compilers.event_prop_compiler import default_params
from ml_genn.utils.data import preprocess_tonic_spikes, linear_latency_encode_data
from argparse import ArgumentParser
from callbacks import CSVLog

import os
import shutil


PreprocessedSpikes = namedtuple("PreprocessedSpikes", ["end_spikes", "spike_times"])

parser = ArgumentParser()
parser.add_argument("--num_hidden", type=int, default=64, help="Number of hidden neurons for MNIST")
parser.add_argument("--sparsity", type=float, default=0.01, help="Sparsity of connections")
parser.add_argument("--delay_min", type=float, default=0.0, help="Initialise delays with this minimum value")
parser.add_argument("--delay_max", type=float, default=0.0, help="Initialise delays with this minimum value")
parser.add_argument("--delays_within", type=int, default=1, help="Have delays within the module")
parser.add_argument("--delay_within_min", type=float, default=0.0, help="Initialise within delays with this minimum value")
parser.add_argument("--delay_within_max", type=float, default=0.0, help="Initialise within delays with this maximum value")
parser.add_argument("--k_reg", type=float, default=5e-11, help="Firing regularisation strength for mnist")
parser.add_argument("--seed", type=int, default=0, help="Random seed")
args = parser.parse_args()
np.random.seed(args.seed)

unique_suffix = "_".join(("_".join(str(i) for i in val) if isinstance(val, list) 
                         else str(val))
                         for arg, val in vars(args).items() if not arg.startswith("__"))

BATCH_SIZE = 256 
NUM_INPUT = 784
NUM_HIDDEN = args.num_hidden
#NUM_OUTPUT = 10
NUM_OUTPUT = 20


class EaseInSchedule(Callback):
    def __init__(self):
        pass
    def set_params(self, compiled_network, **kwargs):
        self._optimiser = compiled_network.optimisers[0][0]
    def on_batch_begin(self, batch):
        # Set parameter to return value of function
        if self._optimiser.alpha < 0.001 :
            self._optimiser.alpha = (self._optimiser.alpha) * (1.05 ** batch)
        else:
            self._optimiser.alpha = 0.001

labels_mnist_test = download_and_parse_mnist_file("t10k-labels-idx1-ubyte.gz", target_dir="../data")
mnist_test_images = download_and_parse_mnist_file("t10k-images-idx3-ubyte.gz", target_dir="../data")
labels_mnist_train = download_and_parse_mnist_file("train-labels-idx1-ubyte.gz", target_dir="../data")
mnist_train_images = download_and_parse_mnist_file("train-images-idx3-ubyte.gz", target_dir="../data")

def linear_latency_encode(data: np.ndarray, max_time: float,
                               min_time: float = 0.0,
                               thresh: int = 1):
    time_range = max_time - min_time
    # Get boolean mask of spiking neurons
    spike_vector = data > thresh

    # Take cumulative sum to get end spikes
    end_spikes = np.cumsum(spike_vector)

    # Extract values of spiking pixels
    spike_pixels = data[spike_vector]

    # Calculate spike times
    spike_times = (((255.0 - spike_pixels) / 255.0) * time_range) + min_time

    # Add to list
    return PreprocessedSpikes(end_spikes, spike_times)


EXAMPLE_TIME = 20.0
DT = 1.0
spikes_mnist_train, spikes_mnist_test =  mnist_train_images, mnist_test_images



def merge_paired_spikes(spikes, labels_orig):
    # Determine the number of pairs based on the smaller dataset
    num_pairs = len(spikes)
    
    # Randomly sample indices for both datasets
    indices_1 = np.random.choice(len(spikes), num_pairs, replace=False)
    indices_2 = np.random.choice(len(spikes), num_pairs, replace=False)
    sequence = np.random.randint(2, size=num_pairs)
    sequenced_spikes_1, sequenced_spikes_2, labels = [], [], []
    for ind_1, ind_2, seq in zip(indices_1, indices_2, sequence):
        sigma = (labels_orig[ind_1] + labels_orig[ind_2]) % 2
        if seq == 1:
            sequenced_spikes_1.append(linear_latency_encode(
                spikes[ind_1],
                EXAMPLE_TIME - (2.0 * DT), 2.0 * DT))
            sequenced_spikes_2.append(linear_latency_encode(
                spikes[ind_2],
                EXAMPLE_TIME + 100.0 - (2.0 * DT), (2.0 + 100.0) * DT))
            labels.append(labels_orig[ind_1] * sigma + (1 - sigma) * (labels_orig[ind_2] + 10))
        else:
            sequenced_spikes_2.append(linear_latency_encode(
                spikes[ind_2],
                EXAMPLE_TIME - (2.0 * DT), 2.0 * DT))
            sequenced_spikes_1.append(linear_latency_encode(
                spikes[ind_1],
                EXAMPLE_TIME + 100.0 - (2.0 * DT), (2.0 + 100.0) * DT))
            sigma = (labels_orig[ind_1] + labels_orig[ind_2]) % 2
            labels.append((labels_orig[ind_2] + 10) * sigma + (1 - sigma) * labels_orig[ind_1])
    return sequenced_spikes_1, sequenced_spikes_2, labels



network = Network(default_params)
with network:
    # Populations
    input_1 = Population(SpikeInput(max_spikes= BATCH_SIZE * 784),
                       NUM_INPUT)
    input_2 = Population(SpikeInput(max_spikes= BATCH_SIZE * 784),
                       NUM_INPUT)
    hidden_1 = Population(LeakyIntegrateFire(v_thresh=1.0, tau_mem=20.0,
                                           tau_refrac=None),
                        NUM_HIDDEN, record_spikes=True)
    hidden_2 = Population(LeakyIntegrateFire(v_thresh=1.0, tau_mem=20.0,
                                           tau_refrac=None),
                        NUM_HIDDEN, record_spikes=True)
    output = Population(LeakyIntegrate(tau_mem=20.0, readout="max_var"),
                        NUM_OUTPUT)

    # Connections
    input_hidden_1 = Connection(input_1, hidden_1, Dense(Normal(mean=0.078, sd=0.045)),
               Exponential(5.0))
    
    input_hidden_2 = Connection(input_2, hidden_2, Dense(Normal(mean=0.078, sd=0.045)),
               Exponential(5.0))

    
    hidden_1_hidden_1 = Connection(hidden_1, hidden_1, Dense(Normal(mean=0.0, sd=0.02), delay=Uniform(args.delay_within_min,args.delay_within_max)),
               Exponential(5.0), max_delay_steps=150)
    hidden_2_hidden_2 = Connection(hidden_2, hidden_2, Dense(Normal(mean=0.0, sd=0.02), delay=Uniform(args.delay_within_min,args.delay_within_max)),
               Exponential(5.0), max_delay_steps=150)
    if args.sparsity == 1.0:
        hidden_1_hidden_2 = Connection(hidden_1, hidden_2, Dense(Normal(mean=0.0, sd=0.02), delay=Uniform(args.delay_min,args.delay_max)),
                Exponential(5.0), max_delay_steps=150)
        hidden_2_hidden_1 = Connection(hidden_2, hidden_1, Dense(Normal(mean=0.0, sd=0.02), delay=Uniform(args.delay_min,args.delay_max)),
                Exponential(5.0), max_delay_steps=150)
    elif args.sparsity > 0.0:
        hidden_1_hidden_2 = Connection(hidden_1, hidden_2, FixedProbability(p=args.sparsity, weight=Normal(mean=0.0, sd=0.02), delay=Uniform(args.delay_min,args.delay_max)),
                Exponential(5.0), max_delay_steps=150)
        if hidden_1_hidden_2.connectivity.pre_ind is None:
            hidden_1_hidden_2.connectivity.pre_ind = [np.random.randint(0, NUM_HIDDEN)]
            hidden_1_hidden_2.connectivity.post_ind = [np.random.randint(0, NUM_HIDDEN)]
        hidden_2_hidden_1 = Connection(hidden_2, hidden_1, FixedProbability(p=args.sparsity, weight=Normal(mean=0.0, sd=0.02), delay=Uniform(args.delay_min,args.delay_max)),
                Exponential(5.0), max_delay_steps=150)
        if hidden_2_hidden_1.connectivity.pre_ind is None:
            hidden_2_hidden_1.connectivity.pre_ind = [np.random.randint(0, NUM_HIDDEN)]
            hidden_2_hidden_1.connectivity.post_ind = [np.random.randint(0, NUM_HIDDEN)]

    '''hidden_output_SSC = Connection(hidden_SSC, output, Dense(Normal(mean=0.0, sd=0.03)),
               Exponential(5.0))
    
    hidden_output_MNIST = Connection(hidden_MNIST, output, Dense(Normal(mean=0.007, sd=0.73)),
               Exponential(5.0))'''
    hidden1_output = Connection(hidden_1, output, FixedProbability(p=0.5, weight=Normal(mean=0.2, sd=0.37)),
               Exponential(5.0))
    pre_ind_1_out, post_ind_1_out = np.meshgrid(np.arange(NUM_HIDDEN), np.arange(10))

    # Flatten the arrays to get 1D arrays of all pairs
    pre_ind_1_out = pre_ind_1_out.flatten() 
    post_ind_1_out = post_ind_1_out.flatten()  
    hidden1_output.connectivity.pre_ind = pre_ind_1_out
    hidden1_output.connectivity.post_ind = post_ind_1_out
    #need to zero out connections from other module
    
    hidden2_output = Connection(hidden_2, output, FixedProbability(p=0.5, weight=Normal(mean=0.2, sd=0.37)),
               Exponential(5.0))

    pre_ind_2_out, post_ind_2_out = np.meshgrid(np.arange(NUM_HIDDEN), np.arange(10,20))
    pre_ind_2_out = pre_ind_2_out.flatten()
    post_ind_2_out = post_ind_2_out.flatten()
    hidden2_output.connectivity.pre_ind = pre_ind_2_out
    hidden2_output.connectivity.post_ind = post_ind_2_out
    

k_reg = {}

k_reg[hidden_1] = args.k_reg
k_reg[hidden_2] = args.k_reg
delay_learn_conns = [hidden_1_hidden_2,hidden_2_hidden_1]
if args.delays_within:
    delay_learn_conns.append(hidden_1_hidden_1)
    delay_learn_conns.append(hidden_2_hidden_2)

max_example_timesteps = int(np.ceil(EXAMPLE_TIME / DT)) +  100

shutil.rmtree("checkpoints_mnist_sequence_" + unique_suffix)
serialiser = Numpy("checkpoints_mnist_sequence_" + unique_suffix)
compiler = EventPropCompiler(example_timesteps=max_example_timesteps,
                                losses="sparse_categorical_crossentropy",
                                reg_lambda_upper=k_reg, reg_lambda_lower=0, 
                                reg_nu_upper=1, max_spikes=1500,
                                delay_learn_conns=delay_learn_conns,
                                optimiser=Adam(0.001 * 0.01), delay_optimiser=Adam(1.0),
                                batch_size=BATCH_SIZE, rng_seed=args.seed)

model_name = (f"classifier_train_{md5(unique_suffix.encode()).hexdigest()}"
                  if os.name == "nt" else f"classifier_train_{unique_suffix}")
compiled_net = compiler.compile(network, name=model_name)

# Apply augmentation to events and preprocess

spikes_train_1, spikes_train_2, labels_train = merge_paired_spikes(spikes_mnist_train, labels_mnist_train)
spikes_test_1, spikes_test_2, labels_test = merge_paired_spikes(spikes_mnist_test, labels_mnist_test)

with compiled_net:
    # Loop through epochs
    callbacks = [CSVLog(f"results/train_output_mnist_sequence_{unique_suffix}.csv", output),  SpikeRecorder(hidden_1, key="hidden_1_spikes", record_counts=True), SpikeRecorder(hidden_2, key="hidden_2_spikes", record_counts=True), EaseInSchedule(), Checkpoint(serialiser)]
    validation_callbacks = [CSVLog(f"results/valid_output_mnist_sequence_{unique_suffix}.csv", output)]
    best_e, best_acc = 0, 0
    early_stop = 15

    for e in range(500):
        
        # Train epoch
        train_metrics, valid_metrics, train_cb, valid_cb  = compiled_net.train({input_1: spikes_train_1, input_2: spikes_train_2},
                                            {output: labels_train},
                                            start_epoch=e, num_epochs=1, 
                                            shuffle=True, callbacks=callbacks, validation_callbacks=validation_callbacks, validation_x={input_1: spikes_test_1, input_2: spikes_test_2}, validation_y={output: labels_test})

        
        
        hidden_1_spikes = np.zeros(NUM_HIDDEN)
        for cb_d in train_cb['hidden_1_spikes']:
            hidden_1_spikes += cb_d
        

        _input_hidden_1 = compiled_net.connection_populations[input_hidden_1]
        _input_hidden_1.vars["g"].pull_from_device()
        g_view = _input_hidden_1.vars["g"].view.reshape((784, NUM_HIDDEN))
        g_view[:,hidden_1_spikes==0] += 0.002
        _input_hidden_1.vars["g"].push_to_device()
        
        hidden_2_spikes = np.zeros(NUM_HIDDEN)
        for cb_d in train_cb['hidden_2_spikes']:
            hidden_2_spikes += cb_d

        _input_hidden_2 = compiled_net.connection_populations[input_hidden_2]
        _input_hidden_2.vars["g"].pull_from_device()
        g_view = _input_hidden_2.vars["g"].view.reshape((784, NUM_HIDDEN))
        g_view[:,hidden_2_spikes==0] += 0.002
        _input_hidden_2.vars["g"].push_to_device()


        
        
        if train_metrics[output].result > best_acc:
            best_acc = train_metrics[output].result
            best_e = e
            early_stop = 15
        else:
            early_stop -= 1
            if early_stop < 0:
                break
        
    compiled_net.save_connectivity((best_e,), serialiser)

    
