'''
Created on Nov 1, 2016

@author: Melvin Laux
'''

#import matplotlib.pyplot as plt
import logging
import pandas as pd
import glob
import numpy as np
import os
import matplotlib.pyplot as plt

logging.basicConfig(level=logging.DEBUG)

PARAM_NAMES = ['acc_bias',
               'miss_bias',
               'short_bias',
               'num_docs',
               'doc_length',
               'group_sizes',
               ]

SCORE_NAMES = ['accuracy',
               'precision-tokens',
               'recall-tokens',
               'f1-score-tokens',
               'auc-score',
               'cross-entropy-error',
               'precision-spans-strict',
               'recall-spans-strict',
               'f1-score-spans-strict',
               'precision-spans-relaxed',
               'recall-spans-relaxed',
               'f1-score-spans-relaxed',
               'count error',
               'number of invalid labels',
               'mean length error'
               ]

def make_plot(methods, param_idx, x_vals, y_vals, x_ticks_labels, ylabel, title='parameter influence'):

    styles = ['-', '--', '-.', ':']
    markers = ['o', 'v', 's', 'p', '*']

    for j in range(len(methods)):
        plt.plot(x_vals, np.mean(y_vals[:, j, :], 1), label=methods[j], ls=styles[j%4], marker=markers[j%5])

    plt.legend(loc='best')

    plt.title(title)
    plt.ylabel(ylabel)
    plt.xlabel(PARAM_NAMES[param_idx])
    plt.xticks(x_vals, x_ticks_labels)
    plt.grid(True)
    plt.grid(True, which='Minor', axis='y')

    if np.min(y_vals) < 0:
        plt.ylim([np.min(y_vals), np.max([1, np.max(y_vals)])])
    else:
        plt.ylim([0, np.max([1, np.max(y_vals)])])


def plot_results(param_values, methods, param_idx, results, show_plot=False, save_plot=False, output_dir='/output/',
                 score_names=None, title='parameter influence'):
    # Plots the results for varying the parameter specified by param_idx

    param_values = np.array(param_values)

    if score_names is None:
        score_names = SCORE_NAMES

    # create output directory if necessary
    if save_plot and not os.path.exists(output_dir):
        os.makedirs(output_dir)

    # initialise values for x axis
    if (param_values.ndim > 1):
        x_vals = param_values[:, 0]
    else:
        x_vals = param_values

    # initialise x-tick labels
    x_ticks_labels = list(map(str, param_values))

    for i in range(len(score_names)):
        make_plot(methods, param_idx, x_vals, results[:,i,:,:], x_ticks_labels, score_names[i], title)

        if save_plot:
            print('Saving plot...')
            plt.savefig(output_dir + 'plot_' + score_names[i] + '.png')
            plt.clf()

        if show_plot:
            plt.show()

        if i == 5:
            make_plot(methods, param_idx, x_vals, results[:,i,:,:], x_ticks_labels, score_names[i], title)
            plt.ylim([0,1])

            if save_plot:
                print('Saving plot...')
                plt.savefig(output_dir + 'plot_' + score_names[i] + '_zoomed' + '.png')
                plt.clf()

            if show_plot:
                plt.show()

def plot_active_learning_results(results_dir, output_dir, result_str='result_'):

    ndocs = np.array([605, 1210, 1815, 2420, 3025, 3630, 4235, 4840, 5445, 6045]) # NER dataset
    methods = np.array([
        'HMM_crowd_then_LSTM',
        'bac_seq_integrateBOF_then_LSTM',
        'bac_seq_integrateBOF_integrateLSTM_atEnd',
    ])

    # results: y-value, metric, methods, runs
    results = np.zeros((len(ndocs), len(SCORE_NAMES), len(methods), 1))
    results_nocrowd = np.zeros((len(ndocs), len(SCORE_NAMES), len(methods), 1))

    resfiles = os.listdir(results_dir)

    for resfile in resfiles:

        if result_str not in resfile:
            continue

        if result_str + 'std_started' in resfile:
            continue
        elif result_str + 'std_nocrowd' in resfile:
            continue

        ndocsidx = int(resfile.split('.csv')[0].split('Nseen')[-1])
        ndocsidx = np.argwhere(ndocs == ndocsidx)[0][0]

        if resfile.split('.')[-1] == 'csv':
            res = pd.read_csv(os.path.join(results_dir, resfile))

            for col in res.columns:
                methodidx = np.argwhere(methods == col.strip("\\# '"))[0][0]

                if result_str + 'started' in resfile:
                    results[ndocsidx, :, methodidx, 0] = res[col]
                elif result_str + 'nocrowd_started' in resfile:
                    results_nocrowd[ndocsidx, :, methodidx, 0] = res[col]

        elif resfile.split('.')[-1] == 'tex':
            res = pd.read_csv(os.path.join(results_dir, resfile), sep='&', header=None)

            for row in range(res.shape[0]):
                methodidx = np.argwhere(methods == res[res.columns[0]][row].strip())[0][0]

                recomputed_order = [6, 7, 8, 3, 4, 5, 13]

                if result_str + 'started' in resfile:
                    for m, metric in enumerate(recomputed_order):
                        results[ndocsidx, metric, methodidx, 0] = res[res.columns[m+1]][row]
                elif result_str + 'nocrowd_started' in resfile:
                    for m, metric in enumerate(recomputed_order):
                        results_nocrowd[ndocsidx, metric, methodidx, 0] = res[res.columns[m+1]][row]

        else:
            continue

    output_pool_dir = os.path.join(output_dir, 'pool/')
    output_test_dir = os.path.join(output_dir, 'test/')

    plot_results(ndocs, methods, 3, results, False, True, output_pool_dir, SCORE_NAMES,
                 title='Active Learning: Pool Data')
    plot_results(ndocs, methods, 3, results_nocrowd, False, True, output_test_dir, SCORE_NAMES,
                 title='Active Learning: Test Data')

if __name__ == '__main__':
    print('Plotting active learning results...')

    results_dir = '../../data/bayesian_annotator_combination/output/good_results/ner_al/'
    output_dir = './documents/figures/NER_AL/'
    if not os.path.isdir(output_dir):
        os.mkdir(output_dir)

    plot_active_learning_results(results_dir, output_dir)#, result_str='recomputed_')
