'''
Created on Oct 19, 2016

@author: Melvin Laux
'''
from src.data.data_generator import DataGenerator
from src.evaluation.experiment import Experiment
import cProfile
import os

profile_out = 'output/profiler/stats'

if not os.path.exists(profile_out):
    os.makedirs(profile_out)

dataGen = DataGenerator('config/data.ini', seed=42)
#exp = Experiment(dataGen, 'config/experiment.ini')
#cProfile.run('exp.run_config()',profile_out+'/test')

acc_exp = 'config/acc_experiment.ini'

#cProfile.run('Experiment(dataGen, acc_exp ).run_config()', profile_out+'/test')

#Experiment(dataGen, 'config/class_bias_experiment.ini').run_config()
#Experiment(dataGen, 'config/crowd_size_experiment.ini').run_config()
#Experiment(dataGen, 'config/short_bias_experiment.ini').run_config()
#Experiment(dataGen, 'config/doc_length_experiment.ini').run_config()
#Experiment(dataGen, 'config/group_ratio_experiment.ini').run_config()
Experiment(dataGen, acc_exp).run_config()


if __name__ == '__main__':
    pass