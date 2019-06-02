from helpers import *
import pandas as pd
import torch
import numpy as np
from crop import Crop
from square_bounding_box import *
from kalman_filters import *
from project_bones import Projection_Client
from PoseEstimationClient import *
from scipy.stats import pearsonr
import pdb

class PoseEstimationClient_Simulation(PoseEstimationClient):
    def __init__(self, energy_param, cropping_tool, pose_client_general, general_param, intrinsics_focal, intrinsics_px, intrinsics_py):
        PoseEstimationClient.__init__(self, energy_param, cropping_tool, general_param["ANIMATION_NUM"], intrinsics_focal, intrinsics_px, intrinsics_py)
        self.simulate_error_mode = True
        self.update_initial_param(pose_client_general)
        self.rewind_step()   

        self.find_best_traj = general_param["FIND_BEST_TRAJ"]
        self.predefined_traj_len = general_param["PREDEFINED_TRAJ_LEN"]

        self.prev_pose = 0        
        self.num_of_noise_trials = general_param["NUM_OF_NOISE_TRIALS"]

        self.frame_overall_error_list = np.zeros([self.num_of_noise_trials,])
        self.frame_future_error_list =  np.zeros([self.num_of_noise_trials,])

        self.correlation_current = []
        self.correlation_future = []
        self.cosine_current = []
        self.cosine_future = []

        self.error_across_trials = [] 
        self.all_average_errors_across_trials = []
        self.final_average_error = 0
    
    def update_initial_param(self, pose_client_general):
        self.init_optimized_poses = pose_client_general.optimized_poses.copy()
        self.init_pose_3d_preoptimization = pose_client_general.pose_3d_preoptimization.copy() 
        self.init_requiredEstimationData = pose_client_general.requiredEstimationData.copy()
        self.init_liftPoseList = pose_client_general.liftPoseList.copy()
        self.init_poses_3d_gt = pose_client_general.poses_3d_gt.copy()
        self.init_middle_pose_error = pose_client_general.middle_pose_error.copy()

        self.init_calib_res_list = pose_client_general.calib_res_list.copy()
        self.init_online_res_list = pose_client_general.online_res_list.copy()
        self.init_processing_time = pose_client_general.processing_time.copy()
        self.init_middle_pose_GT_list = pose_client_general.middle_pose_GT_list.copy()
        self.init_bone_lengths = pose_client_general.boneLengths.clone()
        self.init_multiple_bone_lengths = pose_client_general.multiple_bone_lengths.clone()

        self.init_error_3d = pose_client_general.error_3d.copy()
        self.init_error_2d = pose_client_general.error_2d.copy()

        self.error_across_trials = [] 

        self.rewind_step()       

    def rewind_step(self):
        self.optimized_poses = self.init_optimized_poses.copy()
        self.pose_3d_preoptimization = self.init_pose_3d_preoptimization.copy()
        self.requiredEstimationData = self.init_requiredEstimationData.copy()
        self.liftPoseList = self.init_liftPoseList.copy()
        self.poses_3d_gt = self.init_poses_3d_gt.copy()
        self.boneLengths = self.init_bone_lengths.clone()
        self.multiple_bone_lengths = self.init_multiple_bone_lengths.clone()

        self.middle_pose_error = self.init_middle_pose_error.copy()
        self.error_3d = self.init_error_3d.copy()

        self.calib_res_list = self.init_calib_res_list.copy()
        self.online_res_list = self.init_online_res_list.copy()
        self.processing_time = self.init_processing_time.copy()

        self.error_3d = self.init_error_3d.copy()
        self.error_2d = self.init_error_2d.copy()

    def addNewFrame(self, pose_2d, pose_2d_gt, inv_transformation_matrix, linecount, pose_3d_gt, pose3d_lift):
        self.liftPoseList.insert(0, pose3d_lift.float())
        self.requiredEstimationData.insert(0, [pose_2d, pose_2d_gt, inv_transformation_matrix])

        temp = self.poses_3d_gt[:-1,:].copy() 
        self.poses_3d_gt[0,:] = pose_3d_gt.copy()
        self.poses_3d_gt[1:,:] = temp.copy()
        
    #STH FISHY PAY ATTENTION TO THIS FUNCTION. IT REPEATS IN THE OG
    def update3dPos(self, optimized_poses):
        if (self.isCalibratingEnergy):
            self.current_pose = optimized_poses.copy()
            self.middle_pose = optimized_poses.copy()
            self.future_pose = optimized_poses.copy()
            self.optimized_poses = np.repeat(optimized_poses[np.newaxis, :, :], self.ONLINE_WINDOW_SIZE, axis=0).copy()
        else:
            self.current_pose =  optimized_poses[CURRENT_POSE_INDEX, :,:].copy() #current pose
            self.middle_pose = optimized_poses[MIDDLE_POSE_INDEX, :,:].copy() #middle_pose
            self.future_pose =  optimized_poses[FUTURE_POSE_INDEX, :,:].copy() #future pose
            self.optimized_poses = optimized_poses.copy()

    def update_middle_pose_GT(self, middle_pose):
        pass

    def initialize_pose_3d(self, pose_3d_gt, calculating_future, linecount, pose_2d, inv_transformation_matrix):
        self.pose_3d_preoptimization = self.optimized_poses.copy()

    def append_error(self, trial_ind):
        self.frame_overall_error_list[trial_ind]  = np.mean(np.linalg.norm(self.optimized_poses - self.poses_3d_gt, axis=1))
        self.frame_future_error_list[trial_ind]  = np.mean(np.linalg.norm(self.optimized_poses[0,:,:] - self.poses_3d_gt[0,:,:], axis=0)) 

    def record_noise_experiment_statistics(self, psf, state_ind):
        psf.overall_error_mean_list[state_ind], psf.future_error_mean_list[state_ind], psf.overall_error_std_list[state_ind], psf.future_error_std_list[state_ind] = np.mean(self.frame_overall_error_list), np.mean(self.frame_future_error_list), np.std(self.frame_overall_error_list), np.std(self.frame_future_error_list)
        self.error_across_trials.append(psf.overall_error_mean_list[state_ind])

    def find_correlations(self, psf):
        overall_uncertainty_arr = np.array(list(psf.uncertainty_list_whole.values()), dtype=float)
        norm_overall_uncertainty = (overall_uncertainty_arr-np.min(overall_uncertainty_arr))/(np.max(overall_uncertainty_arr)-np.min(overall_uncertainty_arr))
        norm_overall_error = (psf.overall_error_mean_list-np.min(psf.overall_error_mean_list))/(np.max(psf.overall_error_mean_list)-np.min(psf.overall_error_mean_list))
        self.correlation_current.append(pearsonr(norm_overall_uncertainty, norm_overall_error)[0])
        self.cosine_current.append(norm_overall_uncertainty@norm_overall_error/(np.linalg.norm(norm_overall_uncertainty)*np.linalg.norm(norm_overall_error)))

        future_uncertainty_arr = np.array(list(psf.uncertainty_list_future.values()), dtype=float)
        norm_future_uncertainty = (future_uncertainty_arr-np.min(future_uncertainty_arr))/(np.max(future_uncertainty_arr)-np.min(future_uncertainty_arr))
        norm_future_error = (psf.future_error_mean_list-np.min(psf.future_error_mean_list))/(np.max(psf.future_error_mean_list)-np.min(psf.future_error_mean_list))
        self.correlation_future.append(pearsonr(norm_future_uncertainty, norm_future_error)[0])
        self.cosine_future.append(norm_future_uncertainty@norm_future_error/(np.linalg.norm(norm_future_uncertainty)*np.linalg.norm(norm_future_error)))


    def find_average_error_over_trials(self, index):
        ave_error_of_chosen_index = self.error_across_trials[index]
        self.all_average_errors_across_trials.append(ave_error_of_chosen_index)
        self.final_average_error =  sum(self.all_average_errors_across_trials)/len(self.all_average_errors_across_trials)
        return ave_error_of_chosen_index

    def init_3d_pose(self, pose):
        if self.animation == "noise":
            self.prev_pose = pose.copy()

    def update_internal_3d_pose(self):
        if self.animation == "noise":
            self.prev_pose = add_noise_to_pose(torch.from_numpy(self.prev_pose).float(), self.pose_noise_3d_std).numpy()

    def adjust_3d_pose(self, current_state, pose_client_general):
        if self.animation == "noise":
            current_state.change_human_gt_info(self.prev_pose)
            self.update_bone_lengths(torch.from_numpy(self.prev_pose).float()) #this needs fixing~
            pose_client_general.update_bone_lengths(torch.from_numpy(self.prev_pose).float())

