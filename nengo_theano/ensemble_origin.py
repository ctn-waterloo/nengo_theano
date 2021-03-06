from _collections import OrderedDict

import theano
from theano import tensor as TT
#from theano.tensor.shared_randomstreams import RandomStreams
from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
import numpy as np

from . import cache
from . import neuron
from . import lif
from . import lif_rate
from .origin import Origin

class EnsembleOrigin(Origin):
    def __init__(self, ensemble, dt, func=None, eval_points=None):
        """The output from a population of neurons (ensemble),
        performing a transformation (func) on the represented value.

        :param Ensemble ensemble:
            the Ensemble to which this origin is attached
        :param function func:
            the transformation to perform to the ensemble's
            represented values to get the output value
        
        """
        self.ensemble = ensemble
        # sets up self.decoders
        func_size = self.compute_decoders(func, dt, eval_points) 
        # decoders is array_size * neurons_num * func_dimensions, 
        # initial value should have array_size values * func_dimensions
        initial_value = np.zeros(self.ensemble.array_size * func_size) 
        Origin.__init__(self, func=func, initial_value=initial_value)
        self.func_size = func_size
        self.decoders_shuffled = self.decoders.dimshuffle(0,2,1) 
    
    def compute_decoders(self, func, dt, eval_points=None):     
        """Compute decoding weights.

        Calculate the scaling values to apply to the output
        of each of the neurons in the attached population
        such that the weighted summation of their output
        generates the desired decoded output.

        Decoder values computed as D = (A'A)^-1 A'X_f
        where A is the matrix of activity values of each 
        neuron over sampled X values, and X_f is the vector
        of desired f(x) values across sampled points.

        :param function func: function to compute with this origin
        :param float dt: timestep for simulating to get A matrix
        :param list eval_points:
            specific set of points to optimize decoders over 
        """

        key = self.ensemble.cache_key
        if eval_points == None:  
            # generate sample points from state space randomly
            # to minimize decoder error over in decoder calculation
            #TODO: have num_samples be more for higher dimensions?
            # 5000 maximum (like Nengo)?
            self.num_samples = 500
            eval_points = self.make_samples()

        else:
            # otherwise reset num_samples, and make sure eval_points
            # is in the right form
            # (rows are input dimensions, columns different samples)
            eval_points = np.array(eval_points)
            if len(eval_points.shape) == 1:
                eval_points.shape = [1, eval_points.shape[0]]
            self.num_samples = eval_points.shape[1]

            if eval_points is not self.ensemble.eval_points:
                key += '_eval%08x' % hash(tuple([tuple(x) for x in eval_points]))

            if eval_points.shape[0] != self.ensemble.dimensions: 
                raise Exception("Evaluation points must be of the form: " + 
                    "[dimensions x num_samples]")

        # compute the target_values at the sampled points 
        if func is None:
            # if no function provided, use identity function as default
            target_values = eval_points 
        else:
            # otherwise calculate target_values using provided function
            
            # scale all our sample points by ensemble radius,
            # calculate function value, then scale back to unit length

            # this ensures that we accurately capture the shape of the
            # function when the radius is > 1 (think for example func=x**2)
            target_values = \
                (np.array(
                    [func(s * self.ensemble.radius) for s in eval_points.T]
                    ) / self.ensemble.radius )
            if len(target_values.shape) < 2:
                target_values.shape = target_values.shape[0], 1
            target_values = target_values.T
        eval_points = eval_points.astype('float32')
        
        # replicate attached population of neurons into array of ensembles,
        # one ensemble per sample point
        # set up matrix to store decoders,
        # should be (array_size * neurons_num * dim_func) 
        decoders = np.zeros((self.ensemble.array_size,
                             self.ensemble.neurons_num,
                             target_values.shape[0]))

        for index in range(self.ensemble.array_size): 
            index_key = key + '_%d'%index
            data = cache.get_gamma_inv(index_key)
            if data is not None:
                Ginv, A = data
            else:

                if self.ensemble.neurons.__class__ == lif.LIFNeuron or \
                    self.ensemeble.neurons.__class__ == lif_rate.LIFRateNeurons:

                    # compute the input current for every neuron and every sample point
                    J = np.dot(self.ensemble.encoders[index], eval_points)
                    J += self.ensemble.bias[index][:, np.newaxis]

                    # set up denominator of LIF firing rate equation
                    A = self.ensemble.neurons.tau_ref - self.ensemble.neurons.tau_rc * \
                        np.log(1 - 1.0 / np.maximum(J, 0))
                    
                    # if input current is enough to make neuron spike,
                    # calculate firing rate, else return 0
                    A = np.where(J > 1, 1 / A, 0)

                else:
                    ## This is a generic method for generating an activity matrix
                    ## for any type of neuron model. 

                    # compute the input current for every neuron and every sample point
                    J = TT.dot(self.ensemble.encoders[index], eval_points)
                    J += self.ensemble.bias[index][:, np.newaxis]

                    # so in parallel we can calculate the activity
                    # of all of the neurons at each sample point 
                    neurons = self.ensemble.neurons.__class__(
                        size=(self.ensemble.neurons_num, self.num_samples), 
                        tau_rc=self.ensemble.neurons.tau_rc,
                        tau_ref=self.ensemble.neurons.tau_ref)

                    # run the neuron model for 1 second,
                    # accumulating spikes to get a spike rate
                    #TODO: is this long enough? Should it be less?
                    # If we do less, we may get a good noise approximation!
                    A = neuron.accumulate(J=J, neurons=neurons, dt=dt, 
                        time=dt*200, init_time=dt*20)

                # add noise to elements of A
                # std_dev = max firing rate of population * .1
                noise = .1 # from Nengo
                A += noise * np.random.normal(
                    size=(self.ensemble.neurons_num, self.num_samples), 
                    scale=(self.ensemble.max_rate[1]))

                # compute Gamma and Upsilon
                G = np.dot(A, A.T) # correlation matrix
                
                #TODO: optimize this so we're not doing
                # the full eigenvalue decomposition
                #TODO: add NxS method for large N?
                #TODO: compare below with pinv rcond
                
                #TODO: check the decoder_noise math, and maybe add on to the
                #      diagonal of G?

                # eigh for symmetric matrices, returns
                # evalues w and normalized evectors v
                w, v = np.linalg.eigh(G)

                dnoise = self.ensemble.decoder_noise * \
                    self.ensemble.decoder_noise

                # formerly 0.1 * 0.1 * max(w), set threshold
                limit = dnoise * max(w) 
                v_we_want = np.float32(v[:, w >= limit] / np.sqrt(w[w >= limit]))
                Ginv = np.dot(v_we_want, v_we_want.T)
                
                cache.set_gamma_inv(index_key, (Ginv, A))

            U = np.dot(np.float32(A), np.float32(target_values.T))
            
            # compute decoders - least squares method 
            decoders[index] = np.dot(np.float32(Ginv), np.float32(U))

        self.decoders = theano.shared(decoders.astype('float32'), 
            name='ensemble_origin.decoders')
        return target_values.shape[0]

    def make_samples(self):
        """Generate sample points uniformly distributed within the sphere.
        
        Returns float array of sample points.
        
        """
        np.random.seed(self.ensemble.seed)
        samples = np.random.normal(size=(self.num_samples, self.ensemble.dimensions))

        # normalize magnitude of sampled points to be of unit length
        norm = np.sum(samples * samples, axis=1).reshape(self.num_samples, 1)
        samples /= np.sqrt(norm)
        
        # generate magnitudes for vectors from uniform distribution
        scale = (np.random.uniform(size=(self.num_samples,1))
                 ** (1.0 / self.ensemble.dimensions))

        # scale sample points
        samples *= scale

        return samples.T

    def update(self, dt, spikes):
        """the theano computation for converting neuron output
        into a decoded value.
        
        returns a dictionary with the decoded output value

        :param array spikes:
            theano object representing the instantaneous spike raster
            from the attached population

        """
        # multiply the output by the attached ensemble's radius
        # to put us back in the right range
        r = self.ensemble.radius

        z = TT.zeros((self.ensemble.array_size, self.func_size), dtype='float32')
        for i in range(self.ensemble.array_size):
            z = TT.set_subtensor(z[i], r / dt * TT.dot(spikes[i], 
                self.decoders_shuffled[i].T)) 

        return OrderedDict({self.decoded_output: TT.flatten(z)})
