'''
Error analysis -- cases include:
- Assuming an annotation provided by only one base classifier ( p(O | B or I ) = high for other base classifiers)
- Switching from I-O-O-O to I-O-B-I -- hard to see why this would occur unless transition matrix contains
 p(B | prev_I) < p(B | prev_O) and confusion matrices contain p( I | B ) > p(I | O). 
 Another cause: p( c=O | c_prev = O, t=B) = high (missing an annotation is quite common), 
 p( c=O | c_prev = I, t=B) = low (missing a new annotation straight after another is rare). 
 This error occurs when
 some annotators continue the annotation for longer than others; BAC decides there are two annotations,
 one that agrees with the other combination methods, and one that contains the tail of the overly long
 annotations.  

Possible solution to these two cases is to reduce the prior on missing an annotation, i.e. p( c=O | c_next=whatever, t=B)?
reduce alpha0[2, 1, 1, :], reduce alpha0[2,0,0,:], perhaps increase alpha0[2,2,:,:] to compensate?

Example error:
-- line 2600, argmin binary experiment
-- Some annotators start the annotation early
-- The annotation is split because the B labels are well trusted
-- But the early annotation is marked because there is a high chance of missing annotations by some annotators.
-- Better model might assume lower chance of I-B transition + lower change of missing something?

TODO: task 1 from the HMM Crowd paper looks like it might work well with BAC because Dawid and Skene beats MACE -->
confusion matrix is useful, but HMM_Crowd beats D&S --> sequence is good.

Created on Jan 28, 2017

@author: Melvin Laux
'''
import numpy as np
from scipy.special import logsumexp, psi, gammaln
from scipy.optimize.optimize import fmin
from joblib import Parallel, delayed, cpu_count, effective_n_jobs
import warnings

import lample_lstm_tagger.lstm_wrapper as lstm_wrapper
from lample_lstm_tagger.loader import tag_mapping
from scipy.sparse.coo import coo_matrix


class BAC(object):
    '''
    classdocs
    '''

    K = None  # number of annotators
    L = None  # number of class labels
    
    nscores = None  # number of possible values a token can have, usually this is L + 1 (add one to account for unannotated tokens)
    
    nu0 = None  # ground truth priors
    
    lnA = None  # transition matrix
    lnPi = None  # worker confusion matrices
    
    q_t = None  # current true label estimates
    q_t_old = None  # previous true labels estimates
    
    iter = 0  # current iteration
    
    max_iter = None  # maximum number of iterations
    eps = None  # maximum difference of estimate differences in convergence chack

    def _set_transition_constraints_seqalpha(self):

        if self.tagging_scheme == 'IOB':
            restricted_labels = self.beginning_labels # labels that cannot follow an outside label
            unrestricted_labels = self.inside_labels # labels that can follow any label
        elif self.tagging_scheme == 'IOB2':
            restricted_labels = self.inside_labels
            unrestricted_labels = self.beginning_labels

        # set priors for invalid transitions (to low values)
        for i, restricted_label in enumerate(restricted_labels):
            # pseudo-counts for the transitions that are not allowed from outside to inside
            for outside_label in self.outside_labels:

                # remove transition from outside to restricted label.
                # Move pseudo count to unrestricted label of same type.
                disallowed_count = self.alpha0[:, restricted_label, outside_label, :]
                # pseudocount is (alpha0 - 1) but alpha0 can be < 1. Removing the pseudocount maintains the relative weights between label values
                self.alpha0[:, unrestricted_labels[i], outside_label, :] += disallowed_count

                disallowed_count = self.alpha0_data[:, restricted_label, outside_label, :]
                self.alpha0_data[:, self.beginning_labels[i], outside_label, :] += disallowed_count


                # set the disallowed transition to as close to zero as possible
                self.alpha0[:, restricted_label, outside_label, :] = self.rare_transition_pseudocount
                self.alpha0_data[:, restricted_label, outside_label, :] = self.rare_transition_pseudocount

            #disallowed_count = self.nu0[self.outside_labels, restricted_label]
            #self.nu0[self.outside_labels, unrestricted_labels[i]] += disallowed_count
            if self.nu0.ndim == 2:
                self.nu0[self.outside_labels, restricted_label] = self.rare_transition_pseudocount

            for other_restricted_label in restricted_labels:
                if other_restricted_label == restricted_label:
                    continue

                disallowed_count = self.alpha0[:, restricted_label, other_restricted_label, :]
                # pseudocount is (alpha0 - 1) but alpha0 can be < 1. Removing the pseudocount maintains the relative weights between label values
                self.alpha0[:, other_restricted_label, other_restricted_label, :] += disallowed_count

                disallowed_count = self.alpha0_data[:, restricted_label, other_restricted_label, :]
                self.alpha0_data[:, other_restricted_label, other_restricted_label, :] += disallowed_count

                # set the disallowed transition to as close to zero as possible
                self.alpha0[:, restricted_label, other_restricted_label, :] = self.rare_transition_pseudocount
                self.alpha0_data[:, restricted_label, other_restricted_label, :] = self.rare_transition_pseudocount
                if self.nu0.ndim == 2:
                    self.nu0[other_restricted_label, restricted_label] = self.rare_transition_pseudocount

            for typeid, other_unrestricted_label in enumerate(unrestricted_labels):
                # prevent transitions from unrestricted to restricted if they don't have the same types
                if typeid == i: # same type is allowed
                    continue

                disallowed_count = self.alpha0[:, other_unrestricted_label, restricted_label, :]
                # pseudocount is (alpha0 - 1) but alpha0 can be < 1. Removing the pseudocount maintains the relative weights between label values
                self.alpha0[:, other_unrestricted_label, restricted_label, :] += disallowed_count

                disallowed_count = self.alpha0_data[:, other_unrestricted_label, restricted_label, :]
                self.alpha0_data[:, other_unrestricted_label, other_unrestricted_label, :] += disallowed_count

                # set the disallowed transition to as close to zero as possible
                self.alpha0[:, other_unrestricted_label, restricted_label, :] = self.rare_transition_pseudocount
                self.alpha0_data[:, other_unrestricted_label, restricted_label, :] = self.rare_transition_pseudocount

                if self.nu0.ndim == 2:
                    self.nu0[other_unrestricted_label, restricted_label] = self.rare_transition_pseudocount


        if self.exclusions is not None:
            for label, excluded in dict(self.exclusions).items():
                self.alpha0[:, excluded, label, :] = self.rare_transition_pseudocount
                self.alpha0_data[:, excluded, label, :] = self.rare_transition_pseudocount

                if self.nu0.ndim == 2:
                    self.nu0[label, excluded] = self.rare_transition_pseudocount

    def _set_transition_constraints_nuonly(self):

        if self.nu0.ndim != 2:
            return

        # set priors for invalid transitions (to low values)
        if self.tagging_scheme == 'IOB2':
            for i, inside_label in enumerate(self.inside_labels):
                # pseudo-counts for the transitions that are not allowed from outside to inside
                #disallowed_counts = self.nu0[self.outside_labels, inside_label]
                #self.nu0[self.outside_labels, self.beginning_labels[i]] += disallowed_counts


                self.nu0[self.outside_labels, inside_label] = self.rare_transition_pseudocount

                # cannot jump from one type to another
                for b, begin_label in enumerate(self.beginning_labels):
                    if i == b:
                        continue # this transitiion is allowed
                    self.nu0[begin_label, inside_label] = self.rare_transition_pseudocount

                # can't switch types mid annotation
                for other_inside_label in self.inside_labels:
                    if other_inside_label == inside_label:
                        continue
                    self.nu0[other_inside_label, inside_label] = self.rare_transition_pseudocount

        elif self.tagging_scheme == 'IOB':
            for i, begin_label in enumerate(self.beginning_labels):
                # pseudo-counts for the transitions that are not allowed from outside to inside
                #disallowed_counts = self.nu0[self.outside_labels, begin_label]
                #self.nu0[self.outside_labels, self.inside_labels[i]] += disallowed_counts

                self.nu0[self.outside_labels, begin_label] = self.rare_transition_pseudocount

                # cannot jump from one type to another
                for j, inside_label in enumerate(self.inside_labels):
                    if i == j:
                        continue # this transitiion is allowed

                    self.nu0[inside_label, begin_label] = self.rare_transition_pseudocount

                # if switching types, a B is not used
                for other_b_label in self.beginning_labels:
                    if other_b_label == begin_label:
                        continue
                    self.nu0[other_b_label, begin_label] = self.rare_transition_pseudocount

        if self.exclusions is not None:
                for label, excluded in dict(self.exclusions).items():
                    self.nu0[label, excluded] = self.rare_transition_pseudocount

    def __init__(self, L=3, K=5, max_iter=100, eps=1e-4, inside_labels=[0], outside_labels=[1, -1], beginning_labels=[2],
                 before_doc_idx=1,   exclusions=None, alpha0=None, nu0=None, worker_model='ibcc',
                 data_model=None, alpha0_data=None, tagging_scheme='IOB2', transition_model='HMM'):
        '''
        Constructor

        beginning_labels should correspond in order to inside labels.

        '''
        self.rare_transition_pseudocount = np.min(alpha0) / 10.0 # this makes the rare transition much less likely than
        # any other, but still allows for cases where the data itself may contain errors.
        # self.rare_transition_pseudocount = np.nextafter(0, 1) # use this if the rare transitions are known to be impossible

        self.tagging_scheme = tagging_scheme # may be 'IOB2' (all annotations start with B) or 'IOB' (only annotations
        # that follow another start with B).

        self.L = L
        self.nscores = L
        self.K = K

        self.alpha0 = alpha0
        self.alpha0_data = alpha0_data

        self.inside_labels = inside_labels
        self.outside_labels = outside_labels
        self.beginning_labels = beginning_labels
        self.exclusions = exclusions

        # choose whether to use the HMM transition model or not
        if transition_model == 'HMM':
            if nu0 is None:
                self.nu0 = np.ones((L + 1, L)) * 10
            else:
                self.nu0 = nu0

            self._calc_q_A = self._calc_q_A_trans
            self._update_t = self._update_t_trans
            self._lnpt = self._lnpt_trans
        else:
            if nu0 is None:
                self.nu0 = np.ones(L) * 10
            else:
                self.nu0 = nu0

            self._calc_q_A = self._calc_q_A_notrans
            self._update_t = self._update_t_notrans
            self._lnpt = self._lnpt_notrans

        # choose data model
        if data_model is None:
            self.data_model = ignore_features()
        else:
            self.data_model = data_model()

        # choose type of worker model
        if worker_model == 'acc':
            self.worker_model = AccuracyWorker
            self._set_transition_constraints = self._set_transition_constraints_nuonly
            self.alpha_shape = (2)

        elif worker_model == 'mace':
            self.worker_model = MACEWorker
            self._set_transition_constraints = self._set_transition_constraints_nuonly
            self.alpha_shape = (2 + self.nscores)
            self._lowerbound_pi_terms = self._lowerbound_pi_terms_mace

        elif worker_model == 'ibcc':
            self.worker_model = ConfusionMatrixWorker
            self._set_transition_constraints = self._set_transition_constraints_nuonly
            self.alpha_shape = (self.L, self.nscores)

        elif worker_model == 'seq':
            self.worker_model = SequentialWorker
            self._set_transition_constraints = self._set_transition_constraints_seqalpha
            self.alpha_shape = (self.L, self.nscores)



        self.before_doc_idx = before_doc_idx  # identifies which true class value is assumed for the label before the start of a document
        
        self.max_iter = max_iter  # maximum number of iterations
        self.eps = eps  # threshold for convergence 
        
        self.verbose = False  # can change this if you want progress updates to be printed
        
    def _initA(self):
        self.nu = self.nu0

        if self.nu0.ndim >= 2:
            nu0_sum = psi(np.sum(self.nu0, -1))[:, None]
        else:
            nu0_sum = psi(np.sum(self.nu0))

        self.lnA = psi(self.nu0) - nu0_sum

    def _lowerbound_pi_terms_mace(self):
        # the dimension over which to sum, i.e. over which the values are parameters of a single Dirichlet
        sum_dim = 0 # in the case that we have multiple Dirichlets per worker, e.g. IBCC, sequential-BCC model

        lnpPi_correct = _log_dir(self.alpha0[0:2, :], self.lnPi[0:2, :], sum_dim)
        lnpPi_strategy = _log_dir(self.alpha0[2:, :], self.lnPi[2:, :], sum_dim)

        lnqPi_correct = _log_dir(self.alpha[0:2, :], self.lnPi[0:2, :], sum_dim)
        lnqPi_strategy = _log_dir(self.alpha[2:, :], self.lnPi[2:, :], sum_dim)

        return lnpPi_correct + lnpPi_strategy, lnqPi_correct + lnqPi_strategy

    def _lowerbound_pi_terms(self):
        # the dimension over which to sum, i.e. over which the values are parameters of a single Dirichlet
        if self.alpha.ndim == 2:
            sum_dim = 0 # in the case that we have only one Dirichlet per worker, e.g. accuracy model
        else:
            sum_dim = 1 # in the case that we have multiple Dirichlets per worker, e.g. IBCC, sequential-BCC model

        lnpPi = _log_dir(self.alpha0, self.lnPi, sum_dim)

        lnqPi = _log_dir(self.alpha, self.lnPi, sum_dim)

        return lnpPi, lnqPi

    def _lnpt_trans(self):
        lnpt = self.lnA.copy()[None, :, :]
        lnpt[np.isinf(lnpt) | np.isnan(lnpt)] = 0
        lnpt = lnpt * self.q_t_joint

        return lnpt

    def _lnpt_notrans(self):
        lnpt = self.lnA.copy()[None, :]
        lnpt[np.isinf(lnpt) | np.isnan(lnpt)] = 0
        lnpt = lnpt * self.q_t

        return lnpt

    def lowerbound(self):
        '''
        Compute the variational lower bound on the log marginal likelihood. 
        '''

        lnp_features_and_Cdata = self.data_model.log_likelihood(self.C_data, self.q_t)
        lnq_Cdata = self.C_data * np.log(self.C_data)
        lnq_Cdata[self.C_data == 0] = 0
        lnq_Cdata = np.sum(lnq_Cdata)

        lnpC = 0
        C = self.C.astype(int)
        C_prev = np.concatenate((np.zeros((1, C.shape[1]), dtype=int), C[:-1, :]))
        C_prev[self.doc_start.flatten() == 1, :] = 0
        C_prev[C_prev == 0] = self.before_doc_idx + 1  # document starts or missing labels
        valid_labels = (C != 0).astype(float)
        
        for j in range(self.L):
            # self.lnPi[j, C - 1, C_prev - 1, np.arange(self.K)[None, :]]
            lnpCj = valid_labels * self.worker_model._read_lnPi(self.lnPi, j, C-1, C_prev-1, np.arange(self.K)[None, :], self.nscores) \
                    * self.q_t[:, j:j+1]
            lnpC += lnpCj            

        lnpt = self._lnpt()

        lnpCt = np.sum(lnpC) + np.sum(lnpt)

        # trying to handle warnings        
        qt_sum = np.sum(self.q_t_joint, axis=2)[:, :, None]
        qt_sum[qt_sum==0] = 1.0 # doesn't matter, they will be multiplied by zero. This avoids the warning
        q_t_cond = self.q_t_joint / qt_sum
        q_t_cond[q_t_cond == 0] = 1.0 # doesn't matter, they will be multiplied by zero. This avoids the warning
        lnqt = self.q_t_joint * np.log(q_t_cond)
        lnqt[np.isinf(lnqt) | np.isnan(lnqt)] = 0
        lnqt = np.sum(lnqt) 
        warnings.filterwarnings('always')
            
        # E[ln p(\pi | \alpha_0)]
        # E[ln q(\pi)]
        lnpPi, lnqPi = self._lowerbound_pi_terms()

        # E[ln p(A | nu_0)]
        x = (self.nu0 - 1) * self.lnA
        gammaln_nu0 = gammaln(self.nu0)
        invalid_nus = np.isinf(gammaln_nu0) | np.isinf(x) | np.isnan(x)
        gammaln_nu0[invalid_nus] = 0
        x[invalid_nus] = 0
        x = np.sum(x, axis=1)
        z = gammaln(np.sum(self.nu0, 1) ) - np.sum(gammaln_nu0, 1)
        z[np.isinf(z)] = 0
        lnpA = np.sum(x + z) 
        
        # E[ln q(A)]
        x = (self.nu - 1) * self.lnA
        x[np.isinf(x)] = 0
        gammaln_nu = gammaln(self.nu)
        gammaln_nu[invalid_nus] = 0
        x[invalid_nus] = 0
        x = np.sum(x, axis=1)
        z = gammaln(np.sum(self.nu, 1)) - np.sum(gammaln_nu, 1)
        z[np.isinf(z)] = 0
        lnqA = np.sum(x + z)
        
        print('Computing LB: %f, %f, %f, %f, %f, %f, %f, %f' % (lnpCt, lnqt, lnp_features_and_Cdata, lnq_Cdata,
                                                                lnpPi, lnqPi, lnpA, lnqA))
        lb = lnpCt - lnqt + lnp_features_and_Cdata - lnq_Cdata + lnpPi - lnqPi + lnpA - lnqA
        return lb
        
    def optimize(self, C, doc_start, features=None, maxfun=50, dev_data=None, converge_workers_first=False):
        ''' 
        Run with MLII optimisation over the lower bound on the log-marginal likelihood.
        Optimizes the confusion matrix prior to the same values for all previous labels, and the scaling of the transition matrix
        hyperparameters.
        '''
        self.opt_runs = 0
        
        def neg_marginal_likelihood(hyperparams, C, doc_start):
            # set hyperparameters

            n_alpha_elements = len(hyperparams) - 1

            self.alpha0 = np.exp(hyperparams[0:n_alpha_elements]).reshape(self.alpha_shape)
            self.nu0 = np.ones((self.L + 1, self.L)) * np.exp(hyperparams[-1])

            # run the method
            self.run(C, doc_start, features, dev_data, converge_workers_first=converge_workers_first)
            
            # compute lower bound
            lb = self.lowerbound()
            
            print("Run %i. Lower bound: %.5f, alpha_0 = %s, nu0 scale = %.3f" % (self.opt_runs,
                                                 lb, str(np.exp(hyperparams[0:-1])), np.exp(hyperparams[-1])))

            self.opt_runs += 1
            return -lb
        
        initialguess = np.log(np.append(self.alpha0.flatten(), self.nu0[0, 0]))
        ftol = 1.0  #1e-3
        opt_hyperparams, _, _, _, _ = fmin(neg_marginal_likelihood, initialguess, args=(C, doc_start), maxfun=maxfun,
                                                     full_output=True, ftol=ftol, xtol=1e100) 
            
        print("Optimal hyper-parameters: alpha_0 = %s, nu0 scale = %s" % (np.array2string(np.exp(opt_hyperparams[:-1])),
                                                                          np.array2string(np.exp(opt_hyperparams[-1]))))

        return self.q_t, self._most_probable_sequence(C, doc_start)[1]

    def _update_t_notrans(self, parallel, C, doc_start):

        lnpCT = np.zeros((C.shape[0], self.L))

        Krange = np.arange(self.K)

        # L x 1 x K
        lnPi_terms = self.worker_model._read_lnPi(self.lnPi, None, C[0:1, :] - 1, self.before_doc_idx, Krange[None, :],
                                                  self.nscores)
        lnPi_data_terms = 0
        for m in range(self.nscores):
            lnPi_data_terms = self.worker_model._read_lnPi(self.lnPi_data, None, m, self.before_doc_idx, 0,
                                                           self.nscores)[:, :, 0] * self.C_data[0, m]

        lnpCT[0:1, :] = np.sum(lnPi_terms, axis=2).T + lnPi_data_terms.T

        Cprev = C - 1
        Cprev[Cprev == -1] = self.before_doc_idx
        Cprev = Cprev[:-1, :]
        Ccurr = C[1:, :] - 1

        # L x (N-1) x K
        lnPi_terms = self.worker_model._read_lnPi(self.lnPi, None, C[1:, :] - 1, Cprev, Krange[None, :], self.nscores)
        lnPi_data_terms = 0
        for m in range(self.nscores):
            for n in range(self.nscores):
                lnPi_data_terms += self.worker_model._read_lnPi(self.lnPi_data, None, m, n, 0, self.nscores)[:, :, 0] * \
                                   self.C_data[1:, m][None, :] * self.C_data[:-1, n][None, :]

        lnpCT[1:, :] = np.sum(lnPi_terms, axis=2).T + lnPi_data_terms.T

        lnpCT += self.lnA

        # ensure that the values are not too small
        largest = np.max(lnpCT, 1)[:, np.newaxis]
        joint = lnpCT - largest
        joint = np.exp(joint)
        norma = np.sum(joint, axis=1)[:, np.newaxis]
        self.q_t = joint / norma


    def _update_t_trans(self, parallel, C, doc_start):

        # calculate alphas and betas using forward-backward algorithm
        self.lnR_ = _parallel_forward_pass(parallel, C, self.C_data, self.lnA[0:self.L, :], self.lnPi,
                                           self.lnPi_data, self.lnA[self.before_doc_idx, :], doc_start, self.nscores,
                                           self.worker_model, self.before_doc_idx)

        if self.verbose:
            print("BAC iteration %i: completed forward pass" % self.iter)

        lnLambd = _parallel_backward_pass(parallel, C, self.C_data, self.lnA[0:self.L, :], self.lnPi,
                                          self.lnPi_data, doc_start, self.nscores, self.worker_model,
                                          self.before_doc_idx)
        if self.verbose:
            print("BAC iteration %i: completed backward pass" % self.iter)

        # update q_t and q_t_joint
        self.q_t_joint = _expec_joint_t_quick(self.lnR_, lnLambd, self.lnA, self.lnPi, self.lnPi_data, C,
                                              self.C_data, doc_start, self.nscores, self.worker_model,
                                              self.before_doc_idx)

        self.q_t = np.sum(self.q_t_joint, axis=1)

    def _calc_q_A_trans(self):
        '''
        Update the transition model.
        '''
        self.nu = self.nu0 + np.sum(self.q_t_joint, 0)
        self.q_A = psi(self.nu) - psi(np.sum(self.nu, -1))[:, None]

        if np.any(np.isnan(self.q_A)):
            print('_calc_q_A: nan value encountered!')

    def _calc_q_A_notrans(self):
        '''
        Update the transition model.
        '''
        self.nu = self.nu0 + np.sum(self.q_t, 0)
        self.q_A = psi(self.nu) - psi(np.sum(self.nu))

        if np.any(np.isnan(self.q_A)):
            print('_calc_q_A: nan value encountered!')


    def run(self, C, doc_start, features=None, dev_data=None, converge_workers_first=False):
        '''
        Runs the BAC algorithm with the given annotations and list of document starts.

        '''

        # initialise the hyperparameters to correct sizes
        self.alpha0, self.alpha0_data = self.worker_model._expand_alpha0(self.alpha0, self.alpha0_data, self.K,
                                                                         self.nscores)
        self._set_transition_constraints()

        # initialise transition and confusion matrices
        self._initA()
        self.alpha, self.lnPi = self.worker_model._init_lnPi(self.alpha0)

        self.alpha_data = self.data_model.init(self.alpha0_data, C.shape[0], features, doc_start,
                                                       self.L, dev_data, converge_workers_first)
        self.lnPi_data  = self.worker_model._calc_q_pi(self.alpha_data)

        # validate input data
        assert C.shape[0] == doc_start.shape[0]
        
        # transform input data to desired format: unannotated tokens represented as zeros
        C = C.astype(int) + 1
        doc_start = doc_start.astype(bool)
        
        # initialise variables
        self.iter = 0
        self.q_t_old = np.zeros((C.shape[0], self.L))
        self.q_t = np.ones((C.shape[0], self.L))
        
        oldlb = -np.inf
        
        self.doc_start = doc_start
        self.C = C

        self.C_data = np.zeros((self.C.shape[0], self.nscores)) + (1.0 / self.nscores)

        print('Parallel can run %i jobs simultaneously, with %i cores' % (effective_n_jobs(), cpu_count()) )

        self.data_model_updated = False
        self.workers_converged = False

        # main inference loop
        with Parallel(n_jobs=-1) as parallel:

            while not self._converged() or not self.workers_converged:

                # print status if desired
                if self.verbose:
                    print("BAC iteration %i in progress" % self.iter)

                self.q_t_old = self.q_t

                self._update_t(parallel, C, doc_start)

                if self.verbose:
                    print("BAC iteration %i: computed label sequence probabilities" % self.iter)

                # update E_lnA
                self._calc_q_A()

                if self.verbose:
                    print("BAC iteration %i: updated transition matrix" % self.iter)

                # Update the data model by retraining the integrated task classifier and obtaining its predictions
                if self.iter > -1 and (not converge_workers_first or self.workers_converged):
                    # hold off training the feature-based classifier for three iterations
                    self.C_data = self.data_model.fit_predict(self.q_t)
                    self.data_model_updated = True
                    if self.verbose:
                        print("BAC iteration %i: updated feature-based predictions" % self.iter)

                self.alpha_data = self.worker_model._post_alpha_data(self.q_t, self.C_data, self.alpha0_data,
                                    self.alpha_data, doc_start, self.nscores, self.before_doc_idx)
                self.lnPi_data = self.worker_model._calc_q_pi(self.alpha_data)
                if self.verbose:
                    print("BAC iteration %i: updated model for feature-based predictor" % self.iter)

                # update E_lnpi
                self.alpha = self.worker_model._post_alpha(self.q_t, C, self.alpha0, self.alpha, doc_start,
                                                           self.nscores, self.before_doc_idx)
                self.lnPi = self.worker_model._calc_q_pi(self.alpha)
                if self.verbose:
                    print("BAC iteration %i: updated worker models" % self.iter)

                # Note: we are not using this to check convergence -- it's only here to check correctness of algorithm
                # Can be commented out to save computational costs.
                #lb = self.lowerbound()
                #print('Iter %i, lower bound = %.5f, diff = %.5f' % (self.iter, lb, lb - oldlb))
                #oldlb = lb

                # increase iteration number
                self.iter += 1

            if self.verbose:
                print("BAC iteration %i: computing most probable sequence..." % self.iter)
            seq = self._most_probable_sequence(C, doc_start, parallel)[1]
        if self.verbose:
            print("BAC iteration %i: fitting/predicting complete." % self.iter)

        return self.q_t, seq

    def _most_probable_sequence(self, C, doc_start, parallel):
        '''
        Use Viterbi decoding to ensure we make a valid prediction. There
        are some cases where most probable sequence does not match argmax(self.q_t,1). E.g.:
        [[0.41, 0.4, 0.19], [[0.41, 0.42, 0.17]].
        Taking the most probable labels results in an illegal sequence of [0, 1]. 
        Using most probable sequence would result in [0, 0].
        '''
        if self.nu.ndim >= 2:
            EA = self.nu / np.sum(self.nu, axis=1)[:, None]
        else:
            EA = self.nu / np.sum(self.nu)
            EA = np.tile(EA[None, :], (self.L+1, 1))

        lnEA = np.zeros_like(EA)
        lnEA[EA != 0] = np.log(EA[EA != 0])
        lnEA[EA == 0] = -np.inf

        EPi = self.worker_model._calc_EPi(self.alpha)
        lnEPi = np.zeros_like(EPi)
        lnEPi[EPi != 0] = np.log(EPi[EPi != 0])
        lnEPi[EPi == 0] = -np.inf

        EPi_data = self.worker_model._calc_EPi(self.alpha_data)
        lnEPi_data = np.zeros_like(EPi_data)
        lnEPi_data[EPi_data != 0] = np.log(EPi_data[EPi_data != 0])
        lnEPi_data[EPi_data == 0] = -np.inf

        # split into documents
        docs = np.split(C, np.where(doc_start == 1)[0][1:], axis=0)
        C_data_by_doc = np.split(self.C_data, np.where(doc_start == 1)[0][1:], axis=0)

        # docs = np.split(C, np.where(doc_start == 1)[0][1:], axis=0)
        # run forward pass for each doc concurrently
        res = parallel(delayed(_doc_most_probable_sequence)(doc, C_data_by_doc[d], lnEA, lnEPi, lnEPi_data, self.L,
                        self.nscores, self.K, self.worker_model, self.before_doc_idx) for d, doc in enumerate(docs))
        # reformat results
        pseq = np.concatenate(list(zip(*res))[0], axis=0)
        seq = np.concatenate(list(zip(*res))[1], axis=0)

        return pseq, seq

    def predict(self, doc_start, text):

        C = np.zeros((len(doc_start), self.K), dtype=int) # all blank

        self.C_data = self.data_model.predict(doc_start, text)

        with Parallel(n_jobs=-1) as parallel:

            self.lnR_ = _parallel_forward_pass(parallel, C, self.C_data, self.lnA[0:self.L, :], self.lnPi,
                                               self.lnPi_data, self.lnA[self.before_doc_idx, :], doc_start, self.nscores,
                                               self.worker_model, self.before_doc_idx)

            if self.verbose:
                print("BAC predict: completed forward pass")

            lnLambd = _parallel_backward_pass(parallel, C, self.C_data, self.lnA[0:self.L, :], self.lnPi,
                                              self.lnPi_data, doc_start, self.nscores, self.worker_model,
                                              self.before_doc_idx)
            if self.verbose:
                print("BAC predict: completed backward pass")

            # update q_t and q_t_joint
            q_t_joint = _expec_joint_t_quick(self.lnR_, lnLambd, self.lnA, self.lnPi, self.lnPi_data, C,
                                                  self.C_data, doc_start, self.nscores, self.worker_model,
                                                  self.before_doc_idx)
            if self.verbose:
                print("BAC predict: computed label sequence probabilities")

            q_t = np.sum(q_t_joint, axis=1)

            seq = self._most_probable_sequence(C, doc_start, parallel)[1]

        return q_t, seq

    def _converged(self):
        '''
        Calculates whether the algorithm has _converged or the maximum number of iterations is reached.
        The algorithm has _converged when the maximum difference of an entry of q_t between two iterations is 
        smaller than the given epsilon.
        '''
        if self.verbose:
            print("Difference in values at iteration %i: %.5f" % (self.iter, np.max(np.abs(self.q_t_old - self.q_t))))
        converged = ((self.iter >= self.max_iter) or np.max(np.abs(self.q_t_old - self.q_t)) < self.eps) \
                    and (not self.workers_converged or self.data_model_updated)

        if converged:
            self.workers_converged = True

        return converged

def _log_dir(alpha, lnPi, sum_dim):
    x = (alpha - 1) * lnPi
    gammaln_alpha = gammaln(alpha)
    invalid_alphas = np.isinf(gammaln_alpha) | np.isinf(x) | np.isnan(x)
    gammaln_alpha[invalid_alphas] = 0  # these possibilities should be excluded
    x[invalid_alphas] = 0
    x = np.sum(x, axis=sum_dim)
    z = gammaln(np.sum(alpha, sum_dim)) - np.sum(gammaln_alpha, sum_dim)
    z[np.isinf(z)] = 0
    return np.sum(x + z)

# Worker model: accuracy only ------------------------------------------------------------------------------------------
# lnPi[1] = ln p(correct)
# lnPi[0] = ln p(wrong)

class AccuracyWorker():

    def _init_lnPi(alpha0):

        # Returns the initial values for alpha and lnPi
        psi_alpha_sum = psi(np.sum(alpha0, 0))
        lnPi = psi(alpha0) - psi_alpha_sum[None, :]
        return alpha0, lnPi

    def _calc_q_pi(alpha):
        '''
        Update the annotator models.
        '''
        psi_alpha_sum = psi(np.sum(alpha, 0))[None, :]
        q_pi = psi(alpha) - psi_alpha_sum
        return q_pi

    def _post_alpha(E_t, C, alpha0, alpha, doc_start, nscores, before_doc_idx=-1):  # Posterior Hyperparameters
        '''
        Update alpha.
        '''
        nclasses = E_t.shape[1]
        alpha = alpha0.copy()

        for j in range(nclasses):
            Tj = E_t[:, j]

            correct_count = (C == j + 1).T.dot(Tj).reshape(-1)
            alpha[1, :] += correct_count

            for l in range(nscores):
                if l == j:
                    continue
                incorrect_count = (C == l + 1).T.dot(Tj).reshape(-1)
                alpha[0, :] += incorrect_count

        return alpha

    def _post_alpha_data(E_t, C, alpha0, alpha, doc_start, nscores, before_doc_idx=-1):  # Posterior Hyperparameters
        '''
        Update alpha when C is the votes for one annotator, and each column contains a probability of a vote.
        '''
        nclasses = E_t.shape[1]
        alpha = alpha0.copy()

        for j in range(nclasses):
            Tj = E_t[:, j]

            correct_count = C[:, j:j+1].T.dot(Tj).reshape(-1)
            alpha[1, :] += correct_count

            for l in range(nscores):
                if l == j:
                    continue
                incorrect_count = C[:, l:l+1].T.dot(Tj).reshape(-1)
                alpha[0, :] += incorrect_count

        return alpha

    def _read_lnPi(lnPi, l, C, Cprev, Krange, nscores):

        if np.isscalar(C):
            N = 1
        else:
            N = C.shape[0]

        if np.isscalar(Krange):
            K = 1
        else:
            K = Krange.shape[-1]

        if l is None:
            result = np.zeros((nscores, N, K))
            for l in range(nscores):

                if np.isscalar(C) and l == C:
                    result[l, :, :] = lnPi[1, Krange]

                elif np.isscalar(C) and l != C:
                    result[l, :, :] = lnPi[0, Krange]
                    # incorrect answers: split mass across classes evenly
                    result[l, :, :] = np.log(np.exp(result[l, :, :]) / float(nscores))

                else:
                    idx = (l==C).astype(int)
                    result[l, :, :] = lnPi[idx, Krange]
                    # incorrect answers: split mass across classes evenly
                    result[l, idx == 0] = np.log(np.exp(result[l, idx == 0]) / float(nscores))

            return result

        if np.isscalar(C) and l == C:
            result = lnPi[1, Krange]

        elif np.isscalar(C) and l != C:
            result = lnPi[0, Krange]
            # incorrect answers: split mass across classes evenly
            result = np.log(np.exp(result) / float(nscores))

        else:
            idx = (l==C).astype(int)
            result = lnPi[idx, Krange]
            # incorrect answers: split mass across classes evenly
            result[idx == 0] = np.log(np.exp(result[idx==0]) / float(nscores))

        return result

    def _expand_alpha0(alpha0, alpha0_data, K, nscores):
        '''
        Take the alpha0 for one worker and expand.
        :return:
        '''

        # set priors
        if alpha0 is None:
            # dims: true_label[t], current_annoc[t],  previous_anno c[t-1], annotator k
            alpha0 = np.ones((2, K))
            alpha0[1, :] += 1.0
        else:
            alpha0 = alpha0

        if alpha0_data is None:
            alpha0_data = np.ones((2, 1))
            alpha0_data[1, :] += 1.0

        alpha0 = alpha0[:, None]
        alpha0 = np.tile(alpha0, (1, K))

        return alpha0, alpha0_data

    def _calc_EPi(alpha):
        return alpha / np.sum(alpha, axis=0)[None, :]

# Worker model: MACE-like spammer model --------------------------------------------------------------------------------

# alpha[0,:] and alpha[1,:] are parameters for the spamming probability
# alpha[2:2+nscores,:] are parameters for the spamming pattern
# similarly for lnPi:
# lnPi[1, :] = ln p(correct answer)
# lnPi[0, :] = ln p(incorrect/spam answer)
# lnPi[2:2+nscores, :] = ln p(label given worker is spamming/incorrect)

class MACEWorker():

    def _init_lnPi(alpha0):
        # Returns the initial values for alpha and lnPi

        psi_alpha_sum = np.zeros_like(alpha0)
        psi_alpha_sum[0, :] = psi(alpha0[0,:] + alpha0[1, :])
        psi_alpha_sum[1, :] = psi_alpha_sum[0, :]

        psi_alpha_sum[2:, :] = psi(np.sum(alpha0[2:, :], 0))[None, :]

        lnPi = psi(alpha0) - psi_alpha_sum
        return alpha0, lnPi

    def _calc_q_pi(alpha):
        '''
        Update the annotator models.
        '''
        psi_alpha_sum = np.zeros_like(alpha)
        psi_alpha_sum[0, :] = psi(alpha[0,:] + alpha[1, :])
        psi_alpha_sum[1, :] = psi_alpha_sum[0, :]
        psi_alpha_sum[2:, :] = psi(np.sum(alpha[2:, :], 0))[None, :]

        ElnPi = psi(alpha) - psi_alpha_sum

        # ElnPi[0, :] = np.log(0.5)
        # ElnPi[1, :] = np.log(0.5)
        # ElnPi[2:, :] = np.log(1.0 / float(alpha.shape[1] - 2))

        return ElnPi

    def _post_alpha(E_t, C, alpha0, alpha, doc_start, nscores, before_doc_idx=-1):  # Posterior Hyperparameters
        '''
        Update alpha.
        '''
        # Reusing some equations from the Java MACE implementation.,,
        # strategyMarginal[i,k] = <scalar per worker> p(k knows vs k is spamming for item i | pi, C, E_t)? = ...
        # ... = \sum_j{ E_t[i, j] / (pi[0,k]*pi[2+C[i,k],k] + pi[1,k]*[C[i,k]==j] } * pi[0,k] * pi[2+C[i,k],k]
        # instanceMarginal = \sum_j p(t_i = j) = term used for normalisation
        # spamming = competence = accuracy = pi[1]
        # a = annotator
        # d = item number
        # ai = index of annotator a's annotation for item d
        # goldlabelmarginals[d] = p(C, t_i = j) =   prior(t_i=j) * \prod_k (pi[0,k] * pi[2+C[i,k],k] + pi[1,k] * [C[i,k]==j])
        # [labels[d][ai]] = C[i, :]
        # thetas = pi[2:,:] = strategy params
        # strategyExpectedCounts[a][labels[d][ai]] = pseudo-count for each spamming action = alpha[2+C[i,k], k] += ...
        # ... += strategyMarginal[i,k] / instanceMarginal
        # knowingExpectedCounts[a][0]+=strategyMarginal/instanceMarginal ->alpha[0,k]+=strategyMarginal/instanceMarginal
        # knowingExpectedCounts[a][1] += (goldLabelMarginals[d][labels[d][ai]] * spamming[a][1] / (spamming[a][0] *
        # ...thetas[a][labels[d][ai]] + spamming[a][1])) / instanceMarginal;
        # ... -> alpha[1,k] += E_t[i, C[i,k]] * pi[1,k] / (pi[0,k]*pi[2+C[i,k],k] + pi[1,k]) / instanceMarginal
        # ... everything is normalised by instanceMarginal because goldlabelMarginals is not normalised and is actually
        # a joint probability

        # start by determining the probability of not spamming at each data point using current estimates of pi
        pknowing = 0
        pspamming = 0

        Pi = np.zeros_like(alpha)
        Pi[0, :] = alpha[0, :] / (alpha[0, :] + alpha[1, :])
        Pi[1, :] = alpha[1, :] / (alpha[0, :] + alpha[1, :])
        Pi[2:, :] = alpha[2:, :] / np.sum(alpha[2:, :], 0)[None, :]

        pspamming_j_unnormed = Pi[0, :][None, :] * Pi[C + 1, np.arange(C.shape[1])[None, :]]

        for j in range(E_t.shape[1]):
            Tj = E_t[:, j:j+1]

            pknowing_j_unnormed = (Pi[1,:][None, :] * (C == j + 1))

            pknowing_j = pknowing_j_unnormed / (pknowing_j_unnormed + pspamming_j_unnormed)
            pspamming_j = pspamming_j_unnormed / (pknowing_j_unnormed + pspamming_j_unnormed)

            # The cases where worker has not given a label are not really spam!
            pspamming_j[C==0] = 0

            pknowing += pknowing_j * Tj
            pspamming += pspamming_j * Tj

        correct_count = np.sum(pknowing, 0)
        incorrect_count = np.sum(pspamming, 0)

        alpha = alpha0.copy()
        alpha[1, :] += correct_count
        alpha[0, :] += incorrect_count

        for l in range(nscores):
            strategy_count_l = np.sum((C == l + 1) * pspamming, 0)
            alpha[l+2, :] += strategy_count_l

        return alpha

    def _post_alpha_data(E_t, C, alpha0, alpha, doc_start, nscores, before_doc_idx=-1):  # Posterior Hyperparameters
        '''
        Update alpha when C is the votes for one annotator, and each column contains a probability of a vote.
        '''
        alpha = alpha0.copy()

        # start by determining the probability of not spamming at each data point using current estimates of pi
        pknowing = 0
        pspamming = 0

        Pi = np.zeros_like(alpha)
        Pi[0, :] = alpha[0, :] / (alpha[0, :] + alpha[1, :])
        Pi[1, :] = alpha[1, :] / (alpha[0, :] + alpha[1, :])
        Pi[2:, :] = alpha[2:, :] / np.sum(alpha[2:, :], 0)[None, :]

        pspamming_j_unnormed = 0
        for j in range(C.shape[1]):
            pspamming_j_unnormed += Pi[0, :] * Pi[j, :] * C[:, j:j+1]

        for j in range(E_t.shape[1]):
            Tj = E_t[:, j:j+1]

            pknowing_j_unnormed = (Pi[1,:][None, :] * (C[:, j:j+1]))

            pknowing_j = pknowing_j_unnormed / (pknowing_j_unnormed + pspamming_j_unnormed)
            pspamming_j = pspamming_j_unnormed / (pknowing_j_unnormed + pspamming_j_unnormed)

            pknowing += pknowing_j * Tj
            pspamming += pspamming_j * Tj

        correct_count = np.sum(pknowing, 0)
        incorrect_count = np.sum(pspamming, 0)

        alpha[1, :] += correct_count
        alpha[0, :] += incorrect_count

        for l in range(nscores):
            strategy_count_l = np.sum((C[:, l:l+1]) * pspamming, 0)
            alpha[l+2, :] += strategy_count_l

        return alpha

    def _read_lnPi(lnPi, l, C, Cprev, Krange, nscores):

        ll_incorrect = lnPi[0, Krange] + lnPi[C+2, Krange]

        if np.isscalar(C):
            N = 1
            if C == -1:
                ll_incorrect = 0
        else:
            N = C.shape[0]
            ll_incorrect[C == -1] = 0

        if np.isscalar(Krange):
            K = 1
        else:
            K = Krange.shape[-1]

        if l is None:
            ll_correct = np.zeros((nscores, N, K))
            for m in range(nscores):

                if np.isscalar(C) and C == m:
                    ll_correct[m] = lnPi[1, Krange]

                elif np.isscalar(C) and C != m:
                    ll_correct[m] = - np.inf

                else:
                    idx = (C == m).astype(int)

                    ll_correct[m] = lnPi[1, Krange] * idx
                    ll_correct[m, idx==0] = -np.inf

            ll_incorrect = np.tile(ll_incorrect, (nscores, 1, 1))
        else:
            if np.isscalar(C) and C == l:
                ll_correct = lnPi[1, Krange]

            elif np.isscalar(C) and C != l:
                ll_correct = - np.inf

            else:
                idx = (C == l).astype(int)
                ll_correct = lnPi[1, Krange] * idx
                ll_correct[idx == 0] = - np.inf

        p_correct = np.exp(ll_correct) / (np.exp(ll_correct) + np.exp(ll_incorrect))
        p_incorrect = np.exp(ll_incorrect) / (np.exp(ll_correct) + np.exp(ll_incorrect))

        # deal with infs
        if not np.isscalar(ll_correct):
            ll_correct[p_correct == 0] = 0
        elif p_correct == 0:
            ll_correct = 0

        if not np.isscalar(ll_incorrect):
            ll_incorrect[p_incorrect == 0] = 0
        elif p_incorrect == 0:
            ll_incorrect = 0

        return p_correct * ll_correct + p_incorrect * ll_incorrect

    def _expand_alpha0(alpha0, alpha0_data, K, nscores):
        '''
        Take the alpha0 for one worker and expand.
        :return:
        '''
        L = alpha0.shape[0]

        # set priors
        if alpha0 is None:
            # dims: true_label[t], current_annoc[t],  previous_anno c[t-1], annotator k
            alpha0 = np.ones((nscores + 2, K))
            alpha0[1, :] += 1.0

        if alpha0_data is None:
            alpha0_data = np.ones((nscores + 2, 1))
            alpha0_data[1, :] += 1.0

        alpha0 = alpha0[:, None]
        alpha0 = np.tile(alpha0, (1, K))

        return alpha0, alpha0_data

    def _calc_EPi(alpha):

        pi = np.zeros_like(alpha)

        pi[0] = alpha[0] / (alpha[0] + alpha[1])
        pi[1] = alpha[1] / (alpha[0] + alpha[1])
        pi[2:] = alpha[2:] / np.sum(alpha[2:], axis=0)[None, :]

        return pi

# Worker model: Bayesianized Dawid and Skene confusion matrix ----------------------------------------------------------

class ConfusionMatrixWorker():

    def _init_lnPi(alpha0):
        # Returns the initial values for alpha and lnPi
        psi_alpha_sum = psi(np.sum(alpha0, 1))
        lnPi = psi(alpha0) - psi_alpha_sum[:, None, :]
        return alpha0, lnPi

    def _calc_q_pi(alpha):
        '''
        Update the annotator models.
        '''
        psi_alpha_sum = psi(np.sum(alpha, 1))[:, None, :]
        q_pi = psi(alpha) - psi_alpha_sum
        return q_pi

    def _post_alpha(E_t, C, alpha0, alpha, doc_start, nscores, before_doc_idx=-1):  # Posterior Hyperparameters
        '''
        Update alpha.
        '''
        dims = alpha0.shape
        alpha = alpha0.copy()

        for j in range(dims[0]):
            Tj = E_t[:, j]

            for l in range(dims[1]):
                counts = (C == l + 1).T.dot(Tj).reshape(-1)
                alpha[j, l, :] += counts

        return alpha

    def _post_alpha_data(E_t, C, alpha0, alpha, doc_start, nscores, before_doc_idx=-1):  # Posterior Hyperparameters
        '''
        Update alpha when C is the votes for one annotator, and each column contains a probability of a vote.
        '''
        dims = alpha0.shape
        alpha = alpha0.copy()

        for j in range(dims[0]):
            Tj = E_t[:, j]

            for l in range(dims[1]):
                counts = (C[:, l:l+1]).T.dot(Tj).reshape(-1)
                alpha[j, l, :] += counts

        return alpha

    def _read_lnPi(lnPi, l, C, Cprev, Krange, nscores):
        if l is None:
            if np.isscalar(Krange):
                Krange = np.array([Krange])[None, :]
            if np.isscalar(C):
                C = np.array([C])[:, None]

            result = lnPi[:, C, Krange]
            result[:, C == -1] = 0
        else:
            result = lnPi[l, C, Krange]
            if np.isscalar(C):
                if C == -1:
                    result = 0
            else:
                result[C == -1] = 0

        return result

    def _expand_alpha0(alpha0, alpha0_data, K, nscores):
        '''
        Take the alpha0 for one worker and expand.
        :return:
        '''
        L = alpha0.shape[0]

        # set priors
        if alpha0 is None:
            # dims: true_label[t], current_annoc[t],  previous_anno c[t-1], annotator k
            alpha0 = np.ones((L, nscores, K)) + 1.0 * np.eye(L)[:, :, None]

        if alpha0_data is None:
            alpha0_data = np.ones((L, nscores, 1)) + 1.0 * np.eye(L)[:, :, None]

        alpha0 = alpha0[:, :, None]
        alpha0 = np.tile(alpha0, (1, 1, K))

        return alpha0, alpha0_data

    def _calc_EPi(alpha):
        return alpha / np.sum(alpha, axis=1)[:, None, :]

# Worker model: sequential model of workers-----------------------------------------------------------------------------

class SequentialWorker():

    def _init_lnPi(alpha0):
        # Returns the initial values for alpha and lnPi
        psi_alpha_sum = psi(np.sum(alpha0, 1))
        lnPi = psi(alpha0) - psi_alpha_sum[:, None, :, :]

        return alpha0, lnPi

    def _calc_q_pi(alpha):
        '''
        Update the annotator models.
        '''
        psi_alpha_sum = psi(np.sum(alpha, 1))[:, None, :, :]
        q_pi = psi(alpha) - psi_alpha_sum
        return q_pi

    def _post_alpha(E_t, C, alpha0, alpha, doc_start, nscores, before_doc_idx=-1):  # Posterior Hyperparameters
        '''
        Update alpha.
        '''
        dims = alpha0.shape

        for j in range(dims[0]):
            Tj = E_t[:, j]

            for l in range(dims[1]):
                counts = ((C == l + 1) * doc_start).T.dot(Tj).reshape(-1)
                counts +=  ((C[1:, :] == l + 1) * (C[:-1, :] == 0)).T.dot(Tj[1:]).reshape(-1) # add counts of where
                # previous tokens are missing.

                alpha[j, l, before_doc_idx, :] += counts

                for m in range(dims[1]):
                    counts = ((C == l + 1)[1:, :] * (1 - doc_start[1:]) * (C == m + 1)[:-1, :]).T.dot(Tj[1:]).reshape(-1)
                    alpha[j, l, m, :] += counts

        return alpha

    def _post_alpha_data(E_t, C, alpha0, alpha, doc_start, nscores, before_doc_idx=-1):  # Posterior Hyperparameters
        '''
        Update alpha when C is the votes for one annotator, and each column contains a probability of a vote.
        '''
        dims = alpha0.shape
        alpha = alpha0.copy()

        for j in range(dims[0]):
            Tj = E_t[:, j]

            for l in range(dims[1]):


                counts = ((C[:,l:l+1]) * doc_start).T.dot(Tj).reshape(-1)
                alpha[j, l, before_doc_idx, :] += counts

                for m in range(dims[1]):
                    counts = (C[:, l:l+1][1:, :] * (1 - doc_start[1:]) * C[:, m:m+1][:-1, :]).T.dot(Tj[1:]).reshape(-1)
                    alpha[j, l, m, :] += counts

        return alpha

    def _read_lnPi(lnPi, l, C, Cprev, Krange, nscores):
        if l is None:

            if np.isscalar(Krange):
                Krange = np.array([Krange])[None, :]
            if np.isscalar(C):
                C = np.array([C])[:, None]

            result = lnPi[:, C, Cprev, Krange]
            result[:, C == -1] = 0

        else:
            result = lnPi[l, C, Cprev, Krange]

            if np.isscalar(C):
                if C == -1:
                    result = 0
            else:
                result[C == -1] = 0

        return result

    def _expand_alpha0(alpha0, alpha0_data, K, nscores):
        '''
        Take the alpha0 for one worker and expand.
        :return:
        '''
        L = alpha0.shape[0]

        # set priors
        if alpha0 is None:
            # dims: true_label[t], current_annoc[t],  previous_anno c[t-1], annotator k
            alpha0 = np.ones((L, nscores, nscores + 1, K)) + 1.0 * np.eye(L)[:, :, None, None]

        if alpha0_data is None:
            alpha0_data = np.ones((L, L, L + 1, 1)) + 1.0 * np.eye(L)[:, :, None, None]

        alpha0 = alpha0[:, :, None, None]
        alpha0 = np.tile(alpha0, (1, 1, nscores + 1, K))

        return alpha0, alpha0_data

    def _calc_EPi(alpha):
        return alpha / np.sum(alpha, axis=1)[:, None, :, :]

#-----------------------------------------------------------------------------------------------------------------------

def _expec_t(lnR_, lnLambda):
    '''
    Calculate label probabilities for each token.
    '''
    return np.exp(lnR_ + lnLambda - logsumexp(lnR_ + lnLambda, axis=1)[:, None])

def _expec_joint_t_quick(lnR_, lnLambda, lnA, lnPi, lnPi_data, C, C_data, doc_start, nscores, worker_model, before_doc_idx=-1):
    '''
    Calculate joint label probabilities for each pair of tokens.
    '''
    # initialise variables
    T = lnR_.shape[0]
    L = lnA.shape[-1]
    K = lnPi.shape[-1]

    lnS = np.repeat(lnLambda[:, None, :], L + 1, 1)

    # flags to indicate whether the entries are valid or should be zeroed out later.
    flags = -np.inf * np.ones_like(lnS)
    flags[np.where(doc_start == 1)[0], before_doc_idx, :] = 0
    flags[np.where(doc_start == 0)[0], :, :] = 0
    flags[np.where(doc_start == 0)[0], before_doc_idx, :] = -np.inf

    Cprev = np.append(np.zeros((1, K), dtype=int) + before_doc_idx, C[:-1, :], axis=0)
    Cprev[Cprev == 0] = before_doc_idx

    C_data_prev = np.append(np.zeros((1, L), dtype=int), C_data[:-1, :], axis=0)
    lnR_prev = np.append(np.zeros((1, L)), lnR_[:-1, :], axis=0)

    for l in range(L):

        # print('_expec_joint_t_quick: For class %i: adding likelihoods to Lambdas for doc_starts' % l)

        # for the document starts
        #lnPi[l, C-1, before_doc_idx, np.arange(K)[None, :]]

        lnPi_terms = worker_model._read_lnPi(lnPi, l, C-1, before_doc_idx, np.arange(K)[None, :], nscores)
        lnPi_data_terms = np.zeros(C_data.shape[0], dtype=float)
        for m in range(nscores):
            lnPi_data_terms_m = C_data[:, m] * worker_model._read_lnPi(lnPi_data, l, m, before_doc_idx, 0, nscores)
            lnPi_data_terms_m[np.isnan(lnPi_data_terms_m)] = 0
            lnPi_data_terms += lnPi_data_terms_m

        lnS[:, before_doc_idx, l] += np.sum(lnPi_terms * (C!=0), 1) + lnPi_data_terms + lnA[before_doc_idx, l]

        # print('_expec_joint_t_quick: For class %i: adding likelihoods to Lambdas for all data points ' % l)
        lnPi_terms = worker_model._read_lnPi(lnPi, l, C-1, Cprev-1, np.arange(K)[None, :], nscores)
        lnPi_data_terms = np.zeros(C_data.shape[0], dtype=float)

        for m in range(L):

            weights = C_data[:, m:m+1] * C_data_prev

            for n in range(L):

                lnPi_data_terms_mn = weights[:, n] * worker_model._read_lnPi(lnPi_data, l, m, n, 0, nscores)
                lnPi_data_terms_mn[np.isnan(lnPi_data_terms_mn)] = 0
                lnPi_data_terms += lnPi_data_terms_mn

        loglikelihood_l = (np.sum(lnPi_terms * (C != 0), 1) + lnPi_data_terms)[:, None]

        # for the other times. The document starts will get something invalid written here too, but it will be killed off by the flags
        # print('_expec_joint_t_quick: For class %i: adding the R terms from previous data points' % l)
        lnS[:, :L, l] += loglikelihood_l + lnA[:L, l][None, :] + lnR_prev

    # print('_expec_joint_t_quick: Normalising...')

    # normalise and return
    lnS = lnS + flags
    if np.any(np.isnan(np.exp(lnS))):
        print('_expec_joint_t: nan value encountered (1) ')

    lnS = lnS - logsumexp(lnS, axis=(1, 2))[:, None, None]

    if np.any(np.isnan(np.exp(lnS))):
        print('_expec_joint_t: nan value encountered')

    return np.exp(lnS)


def _doc_forward_pass(C, C_data, lnA, lnPi, lnPi_data, initProbs, nscores, worker_model, before_doc_idx=1, skip=True):
    '''
    Perform the forward pass of the Forward-Backward algorithm (for a single document).
    '''
    T = C.shape[0]  # infer number of tokens
    L = lnA.shape[0]  # infer number of labels
    K = lnPi.shape[-1]  # infer number of annotators
    Krange = np.arange(K)

    # initialise variables
    lnR_ = np.zeros((T, L))

    mask = np.ones_like(C)
    
    if skip:
        mask = (C != 0)

    lnPi_terms = worker_model._read_lnPi(lnPi, None, C[0:1, :] - 1, before_doc_idx, Krange[None, :], nscores)
    lnPi_data_terms = 0
    for m in range(nscores):
        lnPi_data_terms = worker_model._read_lnPi(lnPi_data, None, m, before_doc_idx, 0, nscores)[:, :, 0] * C_data[0, m]

    lnR_[0, :] = initProbs + np.dot(lnPi_terms[:, 0, :], mask[0, :][:, None])[:, 0] + lnPi_data_terms[:, 0]
    lnR_[0, :] = lnR_[0, :] - logsumexp(lnR_[0, :])

    Cprev = C - 1
    Cprev[Cprev == -1] = before_doc_idx
    Cprev = Cprev[:-1, :]
    Ccurr = C[1:, :] -1

    lnPi_terms = worker_model._read_lnPi(lnPi, None, Ccurr, Cprev, Krange[None, :], nscores)
    lnPi_data_terms = 0
    for m in range(nscores):
        for n in range(nscores):
            lnPi_data_terms += worker_model._read_lnPi(lnPi_data, None, m, n, 0, nscores)[:, :, 0] *\
                               C_data[1:, m][None, :] * C_data[:-1, n][None, :]

    likelihood_next = np.sum(mask[None, 1:, :] * lnPi_terms, axis=2) + lnPi_data_terms # L x T-1
    #lnPi[:, Ccurr, Cprev, Krange[None, :]]

    # iterate through all tokens, starting at the beginning going forward
    for t in range(1, T):
        # iterate through all possible labels
        #prev_idx = Cprev[t - 1, :]
        lnR_t = logsumexp(lnR_[t - 1, :][:, None] + lnA, axis=0) + likelihood_next[:, t-1]
        #, np.sum(mask[t, :] * lnPi[:, C[t, :] - 1, prev_idx, Krange], axis=1)

        # normalise
        lnR_[t, :] = lnR_t - logsumexp(lnR_t)
            
    return lnR_

def _parallel_forward_pass(parallel, C, Cdata, lnA, lnPi, lnPi_data, initProbs, doc_start, nscores, worker_model,
                           before_doc_idx=1, skip=True):
    '''
    Perform the forward pass of the Forward-Backward algorithm (for multiple documents in parallel).
    '''
    # split into documents
    C_by_doc = np.split(C, np.where(doc_start == 1)[0][1:], axis=0)
    Cdata_by_doc = np.split(Cdata, np.where(doc_start == 1)[0][1:], axis=0)

    # run forward pass for each doc concurrently
    # option backend='threading' does not work here because of the shared objects locking. Can we release them read-only?
    res = parallel(delayed(_doc_forward_pass)(C_doc, Cdata_by_doc[d], lnA, lnPi, lnPi_data, initProbs,
                                              nscores, worker_model, before_doc_idx, skip)
                   for d, C_doc in enumerate(C_by_doc))

    #res = [_doc_forward_pass(C_doc, Cdata_by_doc[d], lnA, lnPi, lnPi_data, initProbs, nscores, worker_model,
    #                         before_doc_idx, skip)
    #       for d, C_doc in enumerate(C_by_doc)]

    # reformat results
    lnR_ = np.concatenate(res, axis=0)
    
    return lnR_
    
def _doc_backward_pass(C, C_data, lnA, lnPi, lnPi_data, nscores, worker_model, before_doc_idx=1, skip=True):
    '''
    Perform the backward pass of the Forward-Backward algorithm (for a single document).
    '''
    # infer number of tokens, labels, and annotators
    T = C.shape[0]
    L = lnA.shape[0]
    K = lnPi.shape[-1]
    Krange = np.arange(K)

    # initialise variables
    lnLambda = np.zeros((T, L))

    mask = np.ones_like(C)
    
    if skip:
        mask = (C != 0)

    Ccurr = C - 1
    Ccurr[Ccurr == -1] = before_doc_idx
    Ccurr = Ccurr[:-1, :]
    Cnext = C[1:, :] - 1

    C_data_curr = C_data[:-1, :]
    C_data_next = C[1:, :]

    lnPi_terms =  worker_model._read_lnPi(lnPi, None, Cnext, Ccurr, Krange[None, :], nscores)
    lnPi_data_terms = 0

    for m in range(nscores):
        for n in range(nscores):
            terms_mn = worker_model._read_lnPi(lnPi_data, None, m, n, 0, nscores)[:, :, 0] \
                               * C_data_next[:, m][None, :] \
                               * C_data_curr[:, n][None, :]
            terms_mn[:, (C_data_next[:, m] * C_data_curr[:, n]) == 0] = 0
            lnPi_data_terms += terms_mn

    likelihood = np.sum(mask[None, 1:, :] * lnPi_terms, axis=2) + lnPi_data_terms

    # iterate through all tokens, starting at the end going backwards
    for t in range(T - 2, -1, -1):
        #prev_idx = Cprev[t, :]

        # logsumexp over the L classes of the next timestep
        lnLambda_t = logsumexp(lnA + lnLambda[t + 1, :][None, :] + likelihood[:, t][None, :], axis = 1)
        #np.sum(mask[t + 1, :] * lnPi[:, C[t + 1, :] - 1, prev_idx, Krange], axis=1)[None, :], axis = 1)

        # logsumexp over the L classes of the current timestep to normalise
        lnLambda[t] = lnLambda_t - logsumexp(lnLambda_t)

    if(np.any(np.isnan(lnLambda))):
        print('backward pass: nan value encountered at indexes: ')
        print(np.argwhere(np.isnan(lnLambda)))
  
    return lnLambda


def _parallel_backward_pass(parallel, C, C_data, lnA, lnPi, lnPi_data, doc_start, nscores, worker_model,
                            before_doc_idx=1, skip=True):
    '''
    Perform the backward pass of the Forward-Backward algorithm (for multiple documents in parallel).
    '''
    # split into documents
    docs = np.split(C, np.where(doc_start == 1)[0][1:], axis=0)
    C_data_by_doc = np.split(C_data, np.where(doc_start == 1)[0][1:], axis=0)

    # docs = np.split(C, np.where(doc_start == 1)[0][1:], axis=0)
    # run forward pass for each doc concurrently
    res = parallel(delayed(_doc_backward_pass)(doc, C_data_by_doc[d], lnA, lnPi, lnPi_data, nscores, worker_model,
                                               before_doc_idx, skip) for d, doc in enumerate(docs))
    # reformat results
    lnLambda = np.concatenate(res, axis=0)

    return lnLambda

def _doc_most_probable_sequence(C, C_data, lnEA, lnEPi, lnEPi_data, L, nscores, K, worker_model, before_doc_idx):
    lnV = np.zeros((C.shape[0], L))
    prev = np.zeros((C.shape[0], L), dtype=int)  # most likely previous states

    mask = C != 0

    t = 0
    for l in range(L):
        lnPi_terms = worker_model._read_lnPi(lnEPi, l, C[t, :] - 1, before_doc_idx, np.arange(K), nscores)

        lnPi_data_terms = 0
        for m in range(L):
            terms_startm = C_data[t, m] * worker_model._read_lnPi(lnEPi_data, l, m, before_doc_idx, 0, nscores)
            if C_data[t, m] == 0:
                terms_startm = 0

            lnPi_data_terms += terms_startm

        likelihood_current = np.sum(mask[t, :] * lnPi_terms) + lnPi_data_terms

        lnV[t, l] = lnEA[before_doc_idx, l] + likelihood_current

    Cprev = np.copy(C)
    Cprev[C == 0] = before_doc_idx

    lnPi_terms = worker_model._read_lnPi(lnEPi, None, C[1:, :] - 1, Cprev[:-1, :] - 1, np.arange(K), nscores)
    lnPi_data_terms = 0
    for m in range(L):
        for n in range(L):
            weights = (C_data[1:, m] * C_data[:-1, n])[None, :]
            terms_mn = weights * worker_model._read_lnPi(lnEPi_data, None, m, n, 0, nscores)[:, :, 0]
            terms_mn[:, weights[0] == 0] = 0

            lnPi_data_terms += terms_mn

    likelihood_current = np.sum(mask[1:, :][None, :, :] * lnPi_terms, axis=2) + lnPi_data_terms

    for t in range(1, C.shape[0]):
        for l in range(L):
            p_current = lnV[t - 1, :] + lnEA[:L, l] + likelihood_current[l, t-1]
            lnV[t, l] = np.max(p_current)
            prev[t, l] = np.argmax(p_current, axis=0)

    # decode
    seq = np.zeros(C.shape[0], dtype=int)
    pseq = np.zeros((C.shape[0], L), dtype=float)

    t = C.shape[0] - 1

    seq[t] = np.argmax(lnV[t, :])
    pseq[t, :] = lnV[t, :]

    for t in range(C.shape[0] - 2, -1, -1):
        seq[t] = prev[t + 1, seq[t + 1]]
        pseq[t, :] = lnV[t, :] + np.max((pseq[t + 1, :] - lnV[t, prev[t + 1, :]] - lnEA[prev[t + 1, :],
                                            np.arange(lnEA.shape[1])])[None, :] + lnEA[:lnV.shape[1]], axis=1)
        pseq[t, :] = np.exp(pseq[t, :] - logsumexp(pseq[t, :]))

    return pseq, seq

# DATA MODEL -----------------------------------------------------------------------------------------------------------
# Models the likelihood of the features given the class.

class ignore_features:

    def init(self, alpha0_data, N, text, doc_start, nclasses, dev_data, converge_workers_first):
        return np.ones(alpha0_data.shape)

    def fit_predict(self, Et):
        '''
        '''
        return np.ones(Et.shape) / np.float(Et.shape[1])

    def log_likelihood(self, C_data, E_t):
        '''
        '''
        lnp_Cdata = C_data * np.log(C_data)
        lnp_Cdata[C_data == 0] = 0
        return np.sum(lnp_Cdata)

class LSTM:

    def init(self, alpha0_data, N, text, doc_start, nclasses, dev_data, converge_workers_first):

        if converge_workers_first:
            self.n_epochs_per_vb_iter = 20
        else:
            self.n_epochs_per_vb_iter = 1

        self.N = N

        labels = np.zeros(N) # blank at this point. The labels get changed in each VB iteration

        self.sentences, self.IOB_map, self.IOB_label = lstm_wrapper.data_to_lstm_format(N, text, doc_start, labels,
                                                                                    nclasses, include_missing=False)

        self.Ndocs = self.sentences.shape[0]

        self.train_data_objs = None

        self.nclasses = nclasses

        self.dev_sentences = dev_data
        if dev_data is not None:
            self.all_sentences = np.concatenate((self.sentences, self.dev_sentences))
            dev_gold = []
            for sen in self.dev_sentences:
                for tok in sen:
                    dev_gold.append( self.IOB_map[tok[1]] )
            self.dev_labels = dev_gold
        else:
            self.all_sentences = self.sentences

        self.tag_to_id = self.IOB_map
        self.id_to_tag = self.IOB_label

        return alpha0_data

    def fit_predict(self, Et, compute_dev_score=False):
        labels = np.argmax(Et, axis=1)

        l = 0
        labels_by_sen = []
        for s, sen in enumerate(self.sentences):
            sen_labels = []
            labels_by_sen.append(sen_labels)
            for t, tok in enumerate(sen):
                self.sentences[s][t][1] = self.IOB_label[labels[l]]
                sen_labels.append(self.IOB_label[labels[l]])
                l += 1

        # select a random subset of data to use for validation
        if self.dev_sentences is None:
            devidxs = np.random.randint(0, self.Ndocs, int(np.round(self.Ndocs * 0.2)))
            trainidxs = np.ones(self.Ndocs, dtype=bool)
            trainidxs[devidxs] = 0
            train_sentences = self.sentences[trainidxs]

            if len(devidxs) == 0:
                dev_sentences = self.sentences
            else:
                dev_sentences = self.sentences[devidxs]

            dev_labels = np.array(labels_by_sen[devidxs]).flatten()

        else:
            dev_sentences = self.dev_sentences
            train_sentences = self.sentences

            dev_labels = self.dev_labels

        if self.train_data_objs is None:
            self.lstm, self.f_eval, self.train_data_objs = lstm_wrapper.train_LSTM(self.all_sentences, train_sentences,
                                                           dev_sentences, dev_labels, self.IOB_map, self.nclasses, 1,
                                                           self.tag_to_id, self.id_to_tag)
        else:
            n_epochs = self.n_epochs_per_vb_iter # for each bac iteration

            best_dev = -np.inf
            last_score = best_dev
            niter_no_imprv = 0
            max_niter_no_imprv = 1

            for epoch in range(n_epochs):
                niter_no_imprv, best_dev, last_score = lstm_wrapper.run_epoch(0, self.train_data_objs[0], self.train_data_objs[1],
                                    self.train_data_objs[2], self.train_data_objs[3], self.train_data_objs[4],
                                    niter_no_imprv, self.train_data_objs[5], self.train_data_objs[6], self.lstm,
                                    best_dev, last_score, self.nclasses, compute_dev_score, self.IOB_map)

                if niter_no_imprv >= max_niter_no_imprv:
                    print("- early stopping %i epochs without improvement" % niter_no_imprv)
                    break

        # now make predictions for all sentences
        agg, probs = lstm_wrapper.predict_LSTM(self.lstm, self.sentences, self.f_eval, self.nclasses, self.IOB_map)

        print('LSTM assigned class labels %s' % str(np.unique(agg)) )

        return probs

    def predict(self, doc_start, text):
        N = len(doc_start)
        test_sentences, _, _ = lstm_wrapper.data_to_lstm_format(N, text, doc_start,
                                                                np.ones(N), self.nclasses, include_missing=False)

        # now make predictions for all sentences
        agg, probs = lstm_wrapper.predict_LSTM(self.lstm, test_sentences, self.f_eval, self.nclasses, self.IOB_map)

        print('LSTM assigned class labels %s' % str(np.unique(agg)))

        return probs

    def log_likelihood(self, C_data, E_t):
        '''
        '''
        lnp_Cdata = C_data * np.log(E_t)
        lnp_Cdata[E_t == 0] = 0
        return np.sum(lnp_Cdata)

class BagOfFeatures:

    def init(self, alpha0_data, N, text, doc_start, nclasses, dev_data, converge_workers_first):

        self.N = N

        self.nclasses = nclasses

        self.feat_map = {}

        self.features = []

        for feat in text.flatten():
            if feat not in self.feat_map:
                self.feat_map[feat] = len(self.feat_map)

            self.features.append( self.feat_map[feat] )

        self.features = np.array(self.features).astype(int)

        # sparse matrix of one-hot encoding, nfeatures x N
        self.features_mat = coo_matrix((np.ones(len(text)), (self.features, np.arange(N)))).tocsr()

        self.beta0 = np.ones((len(self.feat_map), self.nclasses))

        return alpha0_data

    def fit_predict(self, Et):

        # count the number of occurrences for each label value

        beta = self.beta0 +  self.features_mat.dot(Et)

        self.ElnRho = psi(beta) - psi(np.sum(beta, 0)[None, :])

        lnptext_given_t = self.ElnRho[self.features, :]

        # normalise, assuming equal prior here
        pt_given_text = np.exp(lnptext_given_t - logsumexp(lnptext_given_t, 1)[:, None])

        return pt_given_text

    def predict(self, doc_start, text):
        N = len(doc_start)

        test_features = np.zeros(N, dtype=int)
        valid_feats = np.zeros(N, dtype=bool)

        for i, feat in enumerate(text.flatten()):
            if feat in self.feat_map:
                valid_feats[i] = True
                test_features[i] = self.feat_map[feat]

        lnptext_given_t = self.ElnRho[test_features[valid_feats], :]

        # normalise, assuming equal prior here
        pt_given_text = np.exp(lnptext_given_t - logsumexp(lnptext_given_t, 1)[:, None])

        probs = np.zeros((N, self.nclasses))
        probs[valid_feats, :] = pt_given_text

        return probs

    def log_likelihood(self, C_data, E_t):
        '''
        '''

        lnptext_given_t = self.ElnRho[self.features, :]

        lnp_Cdata = np.sum(E_t * lnptext_given_t)
        lnp_Cdata[E_t == 0] = 0
        return np.sum(lnp_Cdata)