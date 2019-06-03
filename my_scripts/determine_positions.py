from helpers import * 
from State import *
from NonAirSimClient import *
from pose3d_optimizer import *
from pose3d_optimizer_scipy import *
from project_bones import *
import numpy as np
import cv2 as cv
import torch as torch
import time
from scipy.optimize import least_squares
import pdb
import util as demo_util
from PoseEstimationClient import *
from PoseEstimationClient_Simulation import *
from Lift_Client import calculate_bone_directions, calculate_bone_directions_simple, scale_with_bone_lengths

#import openpose as openpose_module
#import liftnet as liftnet_module

objective_online = pose3d_online_parallel_wrapper()
objective_calib = pose3d_calibration_parallel_wrapper()

def adjust_with_M(M, pose, hip_index):
    root_pose = pose[:, hip_index]
    return np.dot(pose - root_pose[:, np.newaxis], M)+root_pose[:, np.newaxis]

def determine_positions(linecount, pose_client, current_state, file_manager):
    if (pose_client.modes["mode_3d"] == "scipy"):
        determine_3d_positions_energy_scipy(linecount, pose_client, current_state, file_manager)
    else:
        print("error! you removed this")
    current_state.update_human_info(pose_client.current_pose)

def determine_2d_positions(pose_client, current_state, return_heatmaps=True, input_image = 0,  scales = [1]):
    mode_2d, cropping_tool = pose_client.modes["mode_2d"], pose_client.cropping_tool

    bone_2d_gt, heatmaps = find_2d_pose_gt (projection_client=pose_client.projection_client, current_state=current_state, input_image=input_image, cropping_tool=cropping_tool, return_heatmaps=return_heatmaps)
    if (mode_2d != "openpose"):
        bone_2d = bone_2d_gt.clone()
        if (mode_2d == "gt_with_noise"):
            bone_2d = add_noise_to_pose(bone_2d, pose_client.NOISE_2D_STD)
    else:            
        bone_2d, heatmaps, _, _ = find_2d_pose_openpose(input_image,  scales)

    pose_client.openpose_error = np.mean(np.linalg.norm(bone_2d_gt-bone_2d, axis=0))
    if not pose_client.USE_SINGLE_JOINT:
        arm_joints, _, _ = return_arm_joints()
        leg_joints, _, _ = return_leg_joints()
        pose_client.openpose_arm_error = np.mean(np.linalg.norm(bone_2d_gt[:, arm_joints]-bone_2d[:, arm_joints], axis=0))
        pose_client.openpose_leg_error = np.mean(np.linalg.norm(bone_2d_gt[:, leg_joints]-bone_2d[:, leg_joints], axis=0))
    return bone_2d, bone_2d_gt.clone(), heatmaps

def find_2d_pose_gt(projection_client, current_state, input_image, cropping_tool, return_heatmaps=True):
    bone_pos_3d_GT, inv_transformation_matrix, _ = current_state.get_frame_parameters()

    pose_2d_torch = projection_client.take_single_projection(torch.from_numpy(bone_pos_3d_GT).float(), inv_transformation_matrix)
    
    pose_2d = pose_2d_torch.detach()
    pose_2d = cropping_tool.crop_pose(pose_2d)

    heatmaps = 0
    if (return_heatmaps):
        heatmaps = create_heatmap(pose_2d.data.cpu().numpy(), input_image.shape[1], input_image.shape[0])

    return pose_2d, heatmaps

def find_2d_pose_openpose(input_image, scales):
    poses, heatmaps, heatmaps_scales, poses_scales = openpose_module.run_only_model(input_image, scales)
    return poses, heatmaps.cpu().numpy(), heatmaps_scales, poses_scales

def find_lifted_pose(pose_2d, cropped_image, heatmap_2d):
    num_of_joints = pose_2d.shape[1]
    pose = torch.cat((torch.t(pose_2d), torch.ones(num_of_joints,1)), 1)
    pose3d_lift, _, _ = liftnet_module.run(cropped_image, heatmap_2d, pose)
    pose3d_lift = pose3d_lift.view(num_of_joints+2,  -1).permute(1, 0)
    pose3d_lift = rearrange_bones_to_mpi(pose3d_lift, is_torch=True)
    return pose3d_lift

def determine_relative_3d_pose(pose_client, current_state, pose_2d, cropped_image, heatmap_2d):
    if not pose_client.USE_LIFT_TERM or pose_client.USE_SINGLE_JOINT:
        return None

    bone_pos_3d_GT, _, transformation_matrix = current_state.get_frame_parameters()
    bone_connections, _, _, hip_index = pose_client.model_settings()

    if (pose_client.modes["mode_lift"] != 'lift'):
        pose3d_relative = torch.from_numpy(bone_pos_3d_GT).clone()
        if (pose_client.modes["mode_lift"] == "gt_with_noise"):
            pose3d_relative = add_noise_to_pose(pose3d_relative, pose_client.NOISE_LIFT_STD)
    elif (pose_client.modes["mode_lift"]   == 'lift'):
        pose3d_lift = find_lifted_pose(pose_2d, cropped_image, heatmap_2d)
        pose3d_relative = pose_client.projection_client.camera_to_world(pose3d_lift.cpu(), transformation_matrix)

    if pose_client.LIFT_METHOD == "complex":
        pose3d_lift_directions = calculate_bone_directions(pose3d_relative, np.array(return_lift_bone_connections(bone_connections)), batch=False) 
    if pose_client.LIFT_METHOD == "simple":
        pose3d_lift_directions = calculate_bone_directions_simple(pose3d_relative, pose_client.boneLengths, pose_client.BONE_LEN_METHOD, np.array(bone_connections), hip_index) 

    return pose3d_lift_directions

def initialize_empty_frames(linecount, pose_client, current_state, file_manager):
    plot_loc, _ = file_manager.plot_loc, file_manager.get_photo_loc()
    bone_connections, joint_names, num_of_joints, hip_index = pose_client.model_settings()
    bone_pos_3d_GT, _, transformation_matrix = current_state.get_frame_parameters()

    #init bone lengths with GT 
    if not pose_client.USE_SINGLE_JOINT:
        pose_client.update_bone_lengths(torch.from_numpy(bone_pos_3d_GT).float())

    pose_2d, _ = prepare_frames_for_optimization(linecount, pose_client, current_state, file_manager, init_empty_frames=True)

    #initial frames
    if pose_client.INIT_POSE_MODE == "gt" or pose_client.INIT_POSE_MODE == "gt_with_noise":
        optimized_poses = bone_pos_3d_GT
    elif pose_client.INIT_POSE_MODE == "zeros":
        optimized_poses = np.zeros([3,num_of_joints])
    elif pose_client.INIT_POSE_MODE == "backproj":
        backprojection_result = pose_client.projection_client.take_single_backprojection(pose_2d, transformation_matrix, joint_names)
        optimized_poses = scale_with_bone_lengths(backprojection_result, pose_client.boneLengths, pose_client.BONE_LEN_METHOD, np.array(bone_connections)).numpy()
        plot_human(bone_pos_3d_GT, optimized_poses, plot_loc, linecount, bone_connections, pose_client.USE_SINGLE_JOINT)

    if not pose_client.isCalibratingEnergy:
        optimized_poses = np.repeat(optimized_poses[np.newaxis, :, :], pose_client.ONLINE_WINDOW_SIZE, axis=0)

    if pose_client.INIT_POSE_MODE == "gt_with_noise":
        optimized_poses = add_noise_to_pose(optimized_poses, pose_client.NOISE_3D_INIT_STD)

    pose_client.update3dPos(optimized_poses)

    #if pose_client.USE_TRAJECTORY_BASIS:
     #   pose_client.optimized_traj[0,:,:] = current_state.bone_pos_gt.copy()


def determine_openpose_error(linecount, pose_client, current_state, file_manager):
    plot_loc, photo_loc = file_manager.plot_loc, file_manager.get_photo_loc()
    bone_pos_3d_GT, inv_transformation_matrix, _ = current_state.get_frame_parameters()
    bone_connections, _, num_of_joints, _ =  pose_client.model_settings()

    input_image = cv.imread(photo_loc)
    cropped_image, scales = pose_client.cropping_tool.crop(input_image, linecount)
    pose_2d, pose_2d_gt, heatmap_2d = determine_2d_positions(pose_client=pose_client, current_state=current_state, return_heatmaps=True, input_image=cropped_image, scales=scales)
    pose3d_lift = determine_relative_3d_pose(pose_client=pose_client, current_state=current_state, pose_2d=pose_2d, cropped_image=cropped_image, heatmap_2d=heatmap_2d)
    pose_2d = pose_client.cropping_tool.uncrop_pose(pose_2d)

    pose_client.update3dPos(bone_pos_3d_GT)

    #plot_human(pose3d_lift.numpy(), pose3d_lift.numpy(), plot_loc, linecount, bone_connections, 0, custom_name="lift_", label_names = ["lift", "lift"])

    plot_end = {"est": bone_pos_3d_GT, "GT": bone_pos_3d_GT, "drone": current_state.C_drone_gt, "eval_time": 0}
    pose_client.append_res(plot_end)
    return pose_2d, pose3d_lift

def prepare_frames_for_optimization(linecount, pose_client, current_state, file_manager, init_empty_frames):
    bone_pos_3d_GT, inv_transformation_matrix, transformation_matrix = current_state.get_frame_parameters()

    input_image = cv.imread(file_manager.get_photo_loc())
    cropped_image, scales = pose_client.cropping_tool.crop(input_image, linecount)

    #find 2d pose (using openpose or gt)
    bone_2d, bone_2d_gt, heatmap_2d = determine_2d_positions(pose_client=pose_client, current_state=current_state, return_heatmaps=True, input_image=cropped_image, scales=scales)

    #find relative 3d pose using liftnet or GT relative pose
    pose3d_lift_directions = determine_relative_3d_pose(pose_client=pose_client, current_state=current_state, pose_2d=bone_2d, cropped_image=cropped_image, heatmap_2d=heatmap_2d)
        
    #uncrop 2d pose
    bone_2d = pose_client.cropping_tool.uncrop_pose(bone_2d)
    bone_2d_gt = pose_client.cropping_tool.uncrop_pose(bone_2d_gt)

    #add current pose as initial pose. if first frame, take backprojection for initialization
    pose_client.set_initial_pose(linecount, bone_pos_3d_GT, bone_2d, transformation_matrix)

    #add information you need to your window
    if not init_empty_frames or (init_empty_frames and pose_client.isCalibratingEnergy):
        pose_client.addNewFrame(bone_2d, bone_2d_gt, inv_transformation_matrix, linecount, bone_pos_3d_GT, pose3d_lift_directions)
    else:
        for _ in range(pose_client.ONLINE_WINDOW_SIZE):
            pose_client.addNewFrame(bone_2d, bone_2d_gt, inv_transformation_matrix, linecount, bone_pos_3d_GT, pose3d_lift_directions)

    return bone_2d, pose3d_lift_directions

def determine_3d_positions_energy_scipy(linecount, pose_client, current_state, file_manager):
    plot_loc, photo_loc = file_manager.plot_loc, file_manager.get_photo_loc()
    bone_connections, joint_names, num_of_joints, hip_index = pose_client.model_settings()
    bone_pos_3d_GT, inv_transformation_matrix, transformation_matrix = current_state.get_frame_parameters()

    bone_2d, pose3d_lift_directions =  prepare_frames_for_optimization(linecount, pose_client, current_state, file_manager, init_empty_frames=False)

    final_loss = np.zeros([1,1])
    result_shape, result_size, loss_dict = pose_client.result_shape, pose_client.result_size, pose_client.loss_dict
    pose3d_init_scrambled = pose_client.pose_3d_preoptimization.copy()

   # if (linecount > 0):
    #calibration mode parameters
    if (pose_client.isCalibratingEnergy): 
        pose3d_init = np.reshape(a = pose3d_init_scrambled, newshape = [result_size,], order = "C")
        objective = objective_calib
        objective_jacobian =  objective_calib.jacobian

    #online mode parameters
    else:
        if pose_client.USE_TRAJECTORY_BASIS:
            pose3d_init = pose_client.optimized_traj.copy()
            pose3d_init = np.reshape(a = pose3d_init, newshape = [result_size,], order = "C")
        else:
            pose3d_init = pose_client.optimized_poses.copy()
            pose3d_init = np.reshape(a = pose3d_init, newshape = [result_size,], order = "C")
        objective = objective_online
        objective_jacobian = objective_online.jacobian

    objective.reset(pose_client)
    start_time = time.time()
    if linecount < 10:
        bounds = (-np.inf, np.inf)
    else:
        bounds = (pose3d_init-1, pose3d_init+1)
    optimized_res = least_squares(objective.forward, pose3d_init, jac=objective_jacobian, bounds=bounds, method=pose_client.method, ftol=pose_client.ftol)
    func_eval_time = time.time() - start_time
    #print("least squares eval time", func_eval_time)
    if not pose_client.USE_TRAJECTORY_BASIS:
        optimized_poses = np.reshape(a = optimized_res.x, newshape = result_shape, order = "C")
    else:
        optimized_traj = np.reshape(a = optimized_res.x, newshape = result_shape, order = "C")
        optimized_poses = project_trajectory(torch.from_numpy(optimized_traj).float(), pose_client.ONLINE_WINDOW_SIZE, pose_client.NUMBER_OF_TRAJ_PARAM).numpy()
        pose_client.optimized_traj = optimized_traj

    if (pose_client.isCalibratingEnergy):
        pose_client.update_bone_lengths(torch.from_numpy(optimized_poses).float())

    pose_client.update3dPos(optimized_poses)

    #if the frame is the first frame, the pose is found through backprojection
    #else:
     #   pose_client.update3dPos(pre_pose_3d)
     #   loss_dict = pose_client.loss_dict_calib
     #   func_eval_time = 0
     #   noisy_init_pose = pre_pose_3d

    pose_client.error_2d.append(final_loss[0])

    adjusted_current_pose = adjust_with_M(pose_client.M, pose_client.current_pose, hip_index)
    #adjusted_future_pose = adjust_with_M(pose_client.M, pose_client.future_pose, hip_index)
    adjusted_middle_pose = adjust_with_M(pose_client.M, pose_client.middle_pose, hip_index)
    check = pose_client.projection_client.take_single_projection(torch.from_numpy(pose_client.current_pose).float(), inv_transformation_matrix)

    #lots of plot stuff
    error_3d = np.mean(np.linalg.norm(bone_pos_3d_GT - adjusted_current_pose, axis=0))
    middle_pose_error = np.mean(np.linalg.norm(pose_client.poses_3d_gt[MIDDLE_POSE_INDEX, :, :] - adjusted_middle_pose, axis=0))

    pose_client.error_3d.append(error_3d)
    pose_client.middle_pose_error.append(middle_pose_error)
    ave_error =  sum(pose_client.error_3d)/len(pose_client.error_3d)
    ave_middle_error =  sum(pose_client.middle_pose_error)/len(pose_client.middle_pose_error)

    if (plot_loc != 0 and not pose_client.quiet and not pose_client.simulate_error_mode): 
        start_plot_time = time.time()
        superimpose_on_image(bone_2d.numpy(), plot_loc, linecount, bone_connections, photo_loc, custom_name="projected_res_", scale = -1, projection=check.numpy())
        #superimpose_on_image(bone_2d.numpy(), plot_loc, linecount, bone_connections, photo_loc, custom_name="projected_res_2_", scale = -1)
        #plot_2d_projection(check.numpy(), plot_loc, linecount, bone_connections, custom_name="proj_2d")

        plot_human(bone_pos_3d_GT, adjusted_current_pose, plot_loc, linecount, bone_connections, pose_client.USE_SINGLE_JOINT, error_3d, additional_text = ave_error)
        #plot_human(bone_pos_3d_GT, noisy_init_pose, plot_loc, linecount, bone_connections, 0, custom_name="init_pose", label_names = ["GT", "Init"])
        #save_heatmaps(heatmap_2d, linecount, plot_loc)
        #save_heatmaps(heatmaps_scales.cpu().numpy(), client.linecount, plot_loc, custom_name = "heatmaps_scales_", scales=scales, poses=poses_scales.cpu().numpy(), bone_connections=bone_connections)
        plot_optimization_losses(objective.pltpts, plot_loc, linecount, loss_dict)

        if (not pose_client.isCalibratingEnergy and not pose_client.simulate_error_mode):
            plot_human(bone_pos_3d_GT, adjusted_current_pose, plot_loc, linecount-MIDDLE_POSE_INDEX+1, bone_connections, pose_client.USE_SINGLE_JOINT, middle_pose_error, custom_name="middle_pose_", label_names = ["GT", "Estimate"], additional_text = ave_middle_error)
            #plot_human(adjusted_current_pose, adjusted_future_pose, plot_loc, linecount, bone_connections, error_3d, custom_name="future_plot_", label_names = ["current", "future"])
            #pose3d_lift_normalized, _ = normalize_pose(pose3d_lift, hip_index, is_torch=False)
            #bone_pos_3d_GT_normalized, _ = normalize_pose(bone_pos_3d_GT, hip_index, is_torch=False)
            #adjusted_current_pose_normalized, _ = normalize_pose(adjusted_current_pose, hip_index, is_torch=False)
            #plot_human(bone_pos_3d_GT_normalized, pose3d_lift_normalized, plot_loc, linecount, bone_connections, error_3d, custom_name="lift_res_", label_names = ["GT", "LiftNet"])
            plot_human(pose3d_lift_directions.numpy(), bone_pos_3d_GT, plot_loc, linecount, bone_connections, error_3d, custom_name="lift_res_", label_names = ["LiftNet", "GT"])
        end_plot_time = time.time()
        print("Time it took to plot", end_plot_time - start_plot_time)
    plot_end = {"est": adjusted_current_pose, "GT": bone_pos_3d_GT, "drone": current_state.C_drone_gt, "eval_time": func_eval_time}
    pose_client.append_res(plot_end)
    file_manager.write_reconstruction_values(adjusted_current_pose, bone_pos_3d_GT, current_state.C_drone_gt, current_state.R_drone_gt, linecount, num_of_joints)


def switch_energy(value):
    pass
