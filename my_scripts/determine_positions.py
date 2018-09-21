from helpers import * 
from State import *
from NonAirSimClient import *
from PoseEstimationClient import *
from pose3d_optimizer import *
from pose3d_optimizer_scipy import *
from project_bones import *
import numpy as np
import torch
import cv2 as cv
from torch.autograd import Variable
import time
from scipy.optimize import least_squares

import util as demo_util

import openpose as openpose_module
import liftnet as liftnet_module

objective_flight = pose3d_flight_scipy()
objective_calib = pose3d_calibration_scipy()

def determine_all_positions(airsim_client, pose_client, measurement_cov_ = 0,  plot_loc = 0, photo_loc = 0, quiet=True):
    if (pose_client.modes["mode_3d"] == 0):
        positions, unreal_positions, cov = determine_3d_positions_all_GT(airsim_client, pose_client, plot_loc, photo_loc, quiet)
    elif (pose_client.modes["mode_3d"] == 1):
        positions, unreal_positions, cov = determine_3d_positions_backprojection(airsim_client, pose_client, measurement_cov_, plot_loc, photo_loc, quiet)
    elif (pose_client.modes["mode_3d"] == 2):            
        positions, unreal_positions, cov = determine_3d_positions_energy_pytorch(airsim_client, pose_client, measurement_cov_, plot_loc, photo_loc, quiet)
    elif (pose_client.modes["mode_3d"] == 3):
        positions, unreal_positions, cov = determine_3d_positions_energy_scipy(airsim_client, pose_client, measurement_cov_, plot_loc, photo_loc, quiet)

    return positions, unreal_positions, cov

def determine_2d_positions(mode_2d, cropping_tool,  return_heatmaps=True, is_torch = True, unreal_positions = 0, bone_pos_3d_GT = 0, input_image = 0,  scales = [1]):
    if (mode_2d == 0):
        bone_2d, heatmaps = find_2d_pose_gt(unreal_positions, bone_pos_3d_GT, input_image, cropping_tool, return_heatmaps, is_torch)
        heatmaps_scales = 0
        poses_scales = 0
    elif (mode_2d == 1):            
        bone_2d, heatmaps, heatmaps_scales, poses_scales = find_2d_pose_openpose(input_image,  scales)
    return bone_2d, heatmaps, heatmaps_scales, poses_scales

def find_2d_pose_gt(unreal_positions, bone_pos_3d_GT, input_image, cropping_tool, return_heatmaps=True, is_torch = True):
    if (is_torch):
        R_drone_unreal = Variable(euler_to_rotation_matrix(unreal_positions[DRONE_ORIENTATION_IND, 0], unreal_positions[DRONE_ORIENTATION_IND, 1], unreal_positions[DRONE_ORIENTATION_IND, 2], returnTensor=True), requires_grad = False) #pitch roll yaw
        C_drone_unreal = Variable(torch.FloatTensor([[unreal_positions[DRONE_POS_IND, 0]],[unreal_positions[DRONE_POS_IND, 1]],[unreal_positions[DRONE_POS_IND, 2]]]), requires_grad = False)
        bone_pos_GT = Variable(torch.from_numpy(bone_pos_3d_GT).float(), requires_grad = True)
        bone_2d_var, heatmaps = take_bone_projection_pytorch(bone_pos_GT, R_drone_unreal, C_drone_unreal)
        bone_2d = bone_2d_var.detach()
        if (return_heatmaps):
            bone_2d = cropping_tool.crop_pose(bone_2d)
            heatmaps = create_heatmap(bone_2d.data.numpy(), input_image.shape[1], input_image.shape[0])
        else:
            heatmaps = 0
    else:
        R_drone_unreal = euler_to_rotation_matrix(unreal_positions[DRONE_ORIENTATION_IND, 0], unreal_positions[DRONE_ORIENTATION_IND, 1], unreal_positions[DRONE_ORIENTATION_IND, 2])
        C_drone_unreal = unreal_positions[DRONE_POS_IND, :]
        C_drone_unreal = C_drone_unreal[:, np.newaxis]
        bone_2d, heatmaps = take_bone_projection(bone_pos_3d_GT, R_drone_unreal, C_drone_unreal)
        if (return_heatmaps):
            bone_2d = cropping_tool.crop_pose(bone_2d)
            heatmaps = create_heatmap(bone_2d, input_image.shape[1], input_image.shape[0])
        else:
            heatmaps = 0
    return bone_2d, heatmaps

def find_2d_pose_openpose(input_image, scales):
    poses, heatmaps, heatmaps_scales, poses_scales = openpose_module.run_only_model(input_image, scales)
    #poses_, heatmaps, _ = openpose_module.run(input_image, scales)
    #poses = torch.from_numpy(poses_[0]).float()
    return poses, heatmaps.cpu().numpy(), heatmaps_scales, poses_scales

def determine_relative_3d_pose(mode_lift, bone_2d, cropped_image, heatmap_2d, R_drone, C_drone, bone_pos_3d_GT):
    if (mode_lift == 0):
        pose3d_relative = bone_pos_3d_GT

    elif (mode_lift == 1):
        num_of_joints = bone_2d.shape[1]
        pose = torch.cat((torch.t(bone_2d), torch.ones(num_of_joints,1)), 1)
        pose3d_lift, _, _ = liftnet_module.run(cropped_image, heatmap_2d, pose)
        pose3d_lift = pose3d_lift.view(num_of_joints+2,  -1).permute(1, 0)
        pose3d_lift = rearrange_bones_to_mpi(pose3d_lift, True)
        pose3d_relative = camera_to_world(R_drone, C_drone, pose3d_lift.cpu().data.numpy(), is_torch = False)
    return pose3d_relative

def determine_3d_positions_energy_scipy(airsim_client, pose_client, measurement_cov_, plot_loc = 0, photo_loc = 0, quiet=True):
    unreal_positions, bone_pos_3d_GT, drone_pos_vec, angle = airsim_client.getSynchronizedData()
    bone_connections, joint_names, num_of_joints, bone_pos_3d_GT = model_settings(pose_client.model, bone_pos_3d_GT)
    input_image = cv.imread(photo_loc)
    cropped_image = pose_client.cropping_tool.crop(input_image)
    scales = pose_client.cropping_tool.scales
    R_drone = euler_to_rotation_matrix(unreal_positions[DRONE_ORIENTATION_IND, 0], unreal_positions[DRONE_ORIENTATION_IND, 1], unreal_positions[DRONE_ORIENTATION_IND, 2])
    C_drone = unreal_positions[DRONE_POS_IND, :]
    C_drone = C_drone[:, np.newaxis]
    cov = transform_cov_matrix(R_drone, measurement_cov_)

    #find 2d pose (using openpose or gt)
    bone_2d, heatmap_2d, _, _ = determine_2d_positions(pose_client.modes["mode_2d"], pose_client.cropping_tool, True, True, unreal_positions, bone_pos_3d_GT, cropped_image, scales)
    if (quiet == False):
        save_heatmaps(heatmap_2d, airsim_client.linecount, plot_loc)
        #save_heatmaps(heatmaps_scales.cpu().numpy(), client.linecount, plot_loc, custom_name = "heatmaps_scales_", scales=scales, poses=poses_scales.cpu().numpy(), bone_connections=bone_connections)

    #find relative 3d pose using liftnet or GT relative pose
    pose3d_lift = determine_relative_3d_pose(pose_client.modes["mode_lift"], bone_2d, cropped_image, heatmap_2d, R_drone, C_drone, bone_pos_3d_GT)
    bone_2d = bone_2d.data.numpy()

    #find liftnet bone directions and save them
    lift_bone_directions = return_lift_bone_connections(bone_connections)
    pose3d_lift_directions = np.zeros([3, len(lift_bone_directions)])
    for i, bone in enumerate(lift_bone_directions):
        bone_vector = pose3d_lift[:, bone[0]] - pose3d_lift[:, bone[1]]
        pose3d_lift_directions[:, i] = bone_vector/(np.linalg.norm(bone_vector)+EPSILON)

    #uncrop 2d pose 
    bone_2d = pose_client.cropping_tool.uncrop_pose(bone_2d)
    pose_client.cropping_tool.update_bbox(numpy_to_tuples(bone_2d))

    #add current pose as initial pose. if first frame, take backprojection for initialization
    if (airsim_client.linecount != 0):
        pre_pose_3d = pose_client.poseList_3d[0]
    else:
       # pre_pose_3d = np.dot(take_bone_backprojection(bone_2d, R_drone, C_drone, joint_names), pose_client.M)
        pre_pose_3d = take_bone_backprojection(bone_2d, R_drone, C_drone, joint_names)
        pose_client.poseList_3d_calibration = pre_pose_3d
       # pose_client.kalman.init_state(np.reshape(a = pre_pose_3d, newshape = [3*num_of_joints,1], order = "F"))

    #add information you need to your window
    if (pose_client.isCalibratingEnergy): 
        pose_client.addNewCalibrationFrame(bone_2d, R_drone, C_drone, pre_pose_3d)
    pose_client.addNewFrame(bone_2d, R_drone, C_drone, pre_pose_3d, pose3d_lift_directions)

    final_loss = np.zeros([1,1])
    if airsim_client.linecount == FRAME_START_OPTIMIZING:
        pose_client.cropping_tool.update_bbox_margin(1)

    if (airsim_client.linecount >0):
        #calibration mode parameters
        if (pose_client.isCalibratingEnergy): 
            loss_dict = CALIBRATION_LOSSES
            data_list = pose_client.requiredEstimationData_calibration
            energy_weights = {"proj":0.5, "sym":0.5}
            objective = objective_calib
            pose3d_init_scrambled = pose_client.poseList_3d_calibration.copy()
            pose3d_init = np.reshape(a = pose3d_init_scrambled, newshape = [3*num_of_joints], order = "C")
            objective.reset(pose_client.model, data_list, energy_weights, loss_dict, pose_client.M)
            objective_jacobian =  objective_calib.jacobian
            result_shape = [3, num_of_joints]
            
        #flight mode parameters
        else:
            loss_dict = LOSSES
            data_list = pose_client.requiredEstimationData
            lift_list = pose_client.liftPoseList
            energy_weights = pose_client.weights
            objective = objective_flight
            objective.reset(pose_client.model, data_list, lift_list, energy_weights, loss_dict, pose_client.FLIGHT_WINDOW_SIZE, pose_client.boneLengths, pose_client.M)
            objective_jacobian = objective_flight.jacobian
            pose3d_init = np.zeros([pose_client.FLIGHT_WINDOW_SIZE, 3, num_of_joints])
            for queue_index, pose3d_ in enumerate(pose_client.poseList_3d):
                pose3d_init[queue_index, :] = pose3d_.copy()
            pose3d_init = np.reshape(a = pose3d_init, newshape = [pose_client.FLIGHT_WINDOW_SIZE*3*num_of_joints,], order = "C")
            result_shape = [pose_client.FLIGHT_WINDOW_SIZE, 3, num_of_joints]

        start_time = time.time()
        optimized_res = least_squares(objective.forward, pose3d_init, jac=objective_jacobian, bounds=(-np.inf, np.inf), method=pose_client.method, ftol=pose_client.ftol)
        func_eval_time = time.time() - start_time
        print("least squares eval time", func_eval_time)
        P_world_scrambled = optimized_res.x

        P_world = np.reshape(a = P_world_scrambled, newshape = result_shape, order = "C")
        inv_hess = np.linalg.inv(objective.hessian(P_world))
        pose_client.updateMeasurementCov(inv_hess)

        #if (pose_client.isCalibratingEnergy):
            #pre_kalman_pose = P_world
            #optimized_3d_pose = P_world
        #else:
            #pre_kalman_pose = P_world[0, :,:]
            #optimized_3d_pose =  P_world[0, :,:]
        #pose_client.kalman.predict()
        #pose_client.kalman.update(z=np.reshape(a = pre_kalman_pose, newshape = [3*num_of_joints,1], order = "F"), noise_cov=cov)
        #pose_client.kalman.update(z=np.reshape(a = pre_kalman_pose, newshape = [3*num_of_joints,1], order = "F"), Hx=objective.forward, HJacobian=objective_jacobian, noise_cov=cov)
        #kalman_res = np.reshape(a = pose_client.kalman.x_post, newshape =[3, num_of_joints], order = "F")

        if (pose_client.isCalibratingEnergy):
            #optimized_3d_pose = kalman_res.copy()
            optimized_3d_pose = P_world
            pose_client.update3dPos(optimized_3d_pose, is_calib = pose_client.isCalibratingEnergy)
            if airsim_client.linecount > 3:
                for i, bone in enumerate(bone_connections):
                    pose_client.boneLengths[i] = np.sum(np.square(optimized_3d_pose[:, bone[0]] - optimized_3d_pose[:, bone[1]]))
        else:
            #P_world[0, :, :] = kalman_res.copy()
            optimized_3d_pose =  P_world[0, :,:]
            pose_client.update3dPos(P_world, is_calib = pose_client.isCalibratingEnergy)
            #optimized_3d_pose = kalman_res.copy()

    #if the frame is the first frame, the energy is found through backprojection
    else:
        optimized_3d_pose = pre_pose_3d
        loss_dict = CALIBRATION_LOSSES
        func_eval_time = 0
    
    pose_client.error_2d.append(final_loss[0])

    check,  _ = take_bone_projection(optimized_3d_pose, R_drone, C_drone)

    #lots of plot stuff
    error_3d = np.mean(np.linalg.norm(bone_pos_3d_GT - optimized_3d_pose, axis=0))
    pose_client.error_3d.append(error_3d)
    if (plot_loc != 0 and not quiet):
        superimpose_on_image(bone_2d, plot_loc, airsim_client.linecount, bone_connections, photo_loc, custom_name="projected_res_", scale = -1, projection=check)
        plot_drone_and_human(bone_pos_3d_GT, np.dot(optimized_3d_pose, pose_client.M), plot_loc, airsim_client.linecount, bone_connections, error_3d)
        plot_matrix(pose_client.measurement_cov, plot_loc, airsim_client.linecount, "covariance", "covariance")
        if (not pose_client.isCalibratingEnergy):
            pose3d_lift_normalized, _ = normalize_pose(pose3d_lift, joint_names, is_torch=False)
            bone_pos_3d_GT_normalized, _ = normalize_pose(bone_pos_3d_GT, joint_names, is_torch=False)
            optimized_3d_pose_normalized, _ = normalize_pose(optimized_3d_pose, joint_names, is_torch=False)
            plot_drone_and_human(bone_pos_3d_GT_normalized, pose3d_lift_normalized, plot_loc, airsim_client.linecount, bone_connections, error_3d, custom_name="lift_res_", label_names = ["GT", "LiftNet"])
            plot_drone_and_human(pose3d_lift_normalized, optimized_3d_pose_normalized, plot_loc, airsim_client.linecount, bone_connections, error_3d, custom_name="lift_res_2_", label_names = ["LiftNet", "Estimate"])
            plot_optimization_losses(objective.pltpts, plot_loc, airsim_client.linecount, loss_dict)

    positions = form_positions_dict(angle, drone_pos_vec, optimized_3d_pose[:,joint_names.index('spine1')])
    f_output_str = '\t'+str(unreal_positions[HUMAN_POS_IND, 0]) +'\t'+str(unreal_positions[HUMAN_POS_IND, 1])+'\t'+str(unreal_positions[HUMAN_POS_IND, 2])+'\t'+str(angle[0])+'\t'+str(angle[1])+'\t'+str(angle[2])+'\t'+str(drone_pos_vec.x_val)+'\t'+str(drone_pos_vec.y_val)+'\t'+str(drone_pos_vec.z_val)
    plot_end = {"est": optimized_3d_pose, "GT": bone_pos_3d_GT, "drone": C_drone, "eval_time": func_eval_time, "f_string": f_output_str}
    pose_client.append_res(plot_end)

    return positions, unreal_positions, cov


def determine_3d_positions_energy_pytorch(airsim_client, pose_client, measurement_cov_, plot_loc = 0, photo_loc = 0, quiet=False):
    unreal_positions, bone_pos_3d_GT, drone_pos_vec, angle = airsim_client.getSynchronizedData()
    bone_connections, joint_names, num_of_joints, bone_pos_3d_GT = model_settings(pose_client.model, bone_pos_3d_GT)
    input_image = cv.imread(photo_loc)
    cropped_image = pose_client.cropping_tool.crop(input_image)
    scales = pose_client.cropping_tool.scales

    #DONT FORGET THESE CHANGES
    R_drone = Variable(euler_to_rotation_matrix(unreal_positions[DRONE_ORIENTATION_IND, 0], unreal_positions[DRONE_ORIENTATION_IND, 1], unreal_positions[DRONE_ORIENTATION_IND, 2], returnTensor=True), requires_grad = False)
    C_drone = Variable(torch.FloatTensor([[unreal_positions[DRONE_POS_IND, 0]],[unreal_positions[DRONE_POS_IND, 1]],[unreal_positions[DRONE_POS_IND, 2]]]), requires_grad = False)

    #find 2d pose (using openpose or gt)
    bone_2d, heatmap_2d, _, _ = determine_2d_positions(pose_client.modes["mode_2d"], pose_client.cropping_tool, True, True, unreal_positions, bone_pos_3d_GT, cropped_image, scales)

    #find 3d pose using liftnet
    pose = torch.cat((torch.t(bone_2d), torch.ones(num_of_joints,1)), 1)
    input_image = cv.imread(photo_loc)
    pose3d_lift, _, _ = liftnet_module.run(input_image, heatmap_2d.cpu().numpy(), pose)
    pose3d_lift = pose3d_lift.view(num_of_joints+2,  -1).permute(1, 0)
    pose3d_lift = rearrange_bones_to_mpi(pose3d_lift, True)
    pose3d_lift = camera_to_world(R_drone, C_drone, pose3d_lift.cpu(), is_torch = True)

    #find lift bone directions
    pose3d_lift_directions = torch.zeros([3, num_of_joints-1])
    for i, bone in enumerate(bone_connections):
        bone_vector = pose3d_lift[:, bone[0]] - pose3d_lift[:, bone[1]]
        pose3d_lift_directions[:, i] = bone_vector/(torch.norm(bone_vector)+EPSILON)

    if (airsim_client.linecount != 0):
        pose3d_ = pose_client.poseList_3d[-1]
    else:
        pose3d_ = take_bone_backprojection_pytorch(bone_2d, R_drone, C_drone, joint_names)

    if (pose_client.isCalibratingEnergy): 
        pose_client.addNewCalibrationFrame(bone_2d, R_drone, C_drone, pose3d_)
    pose_client.addNewFrame(bone_2d, R_drone, C_drone, pose3d_, pose3d_lift_directions)

    pltpts = {}
    final_loss = np.zeros([1,1])
    if (airsim_client.linecount >1):
        #calibration mode parameters
        if (pose_client.isCalibratingEnergy): 
            objective = pose3d_calibration(pose_client.model)
            optimizer = torch.optim.SGD(objective.parameters(), lr = 0.0005, momentum=0.9)
            #optimizer = torch.optim.LBFGS(objective.parameters(), lr = 0.0005, max_iter=50)
            if (airsim_client.linecount < 8):
                num_iterations = 5000
            else:
                num_iterations = 500
            objective.init_pose3d(pose3d_) 
            loss_dict = CALIBRATION_LOSSES
            data_list = pose_client.requiredEstimationData_calibration
            energy_weights = {"proj":0.5, "sym":0.5}
         #flight mode parameters
        else:
            objective = pose3d_flight(pose_client.boneLengths, pose_client.FLIGHT_WINDOW_SIZE, pose_client.model)
            optimizer = torch.optim.SGD(objective.parameters(), lr =1, momentum=0.8)
            #optimizer = torch.optim.LBFGS(objective.parameters(), lr = 0.0005,  max_iter=50)
            num_iterations = 5000
            #init all 3d pose 
            for queue_index, pose3d_ in enumerate(pose_client.poseList_3d):
                objective.init_pose3d(pose3d_, queue_index)
            loss_dict = LOSSES
            data_list = pose_client.requiredEstimationData
            energy_weights = pose_client.weights

        for loss_key in loss_dict:
            pltpts[loss_key] = np.zeros([num_iterations])

        start_time = time.time()
        for i in range(num_iterations):
            def closure():
                outputs = {}
                output = {}
                for loss_key in loss_dict:
                    outputs[loss_key] = []
                    output[loss_key] = 0
                optimizer.zero_grad()
                objective.zero_grad()

                queue_index = 0
                for bone_2d_, R_drone_, C_drone_ in data_list:
                    if (pose_client.isCalibratingEnergy):
                        loss = objective.forward(bone_2d_, R_drone_, C_drone_)
                    else: 
                        pose3d_lift_directions = pose_client.liftPoseList[queue_index]
                        loss = objective.forward(bone_2d_, R_drone_, C_drone_, pose3d_lift_directions, queue_index)

                        if (i == num_iterations - 1 and queue_index == 0):
                            normalized_pose_3d, _ = normalize_pose(objective.pose3d[0, :, :], joint_names, is_torch = True)
                            normalized_lift, _ = normalize_pose(pose3d_lift, joint_names, is_torch = True)
                            bone_pos_3d_GT
                            plot_drone_and_human(normalized_pose_3d.data.numpy(), normalized_lift.cpu().numpy(), plot_loc, airsim_client.linecount, bone_connections, custom_name="lift_res_", orientation = "z_up")
                    for loss_key in loss_dict:
                        outputs[loss_key].append(loss[loss_key])
                    queue_index += 1

                overall_output = Variable(torch.FloatTensor([0]))
                for loss_key in loss_dict:
                    output[loss_key] = (sum(outputs[loss_key])/len(outputs[loss_key]))
                    overall_output += energy_weights[loss_key]*output[loss_key]
                    pltpts[loss_key][i] = output[loss_key].data.numpy() 
                    if (i == num_iterations - 1):
                        final_loss[0] += energy_weights[loss_key]*np.copy(output[loss_key].data.numpy())

                overall_output.backward(retain_graph = True)
                return overall_output
            optimizer.step(closure)

        func_eval_time = time.time() - start_time
        print("pytorch eval time", func_eval_time)

        if (pose_client.isCalibratingEnergy):
            P_world = objective.pose3d
            pose_client.update3dPos(P_world, all = True)
            if airsim_client.linecount > 3:
                for i, bone in enumerate(bone_connections):
                    pose_client.boneLengths[i] = torch.sum(torch.pow(P_world[:, bone[0]] - P_world[:, bone[1]],2)).data 
                update_torso_size(0.86*(torch.sqrt(torch.sum(torch.pow(P_world[:, joint_names.index('neck')].data- P_world[:, joint_names.index('spine1')].data, 2)))))   
        else:
            P_world = objective.pose3d[0, :, :]
            pose_client.update3dPos(P_world)

    #if the frame is the first frame, the energy is found through backprojection
    else:
        P_world = pose3d_
        loss_dict = CALIBRATION_LOSSES
        func_eval_time = 0
    
    pose_client.error_2d.append(final_loss[0])
    #check,  _ = take_bone_projection_pytorch(P_world, R_drone, C_drone)

    P_world = P_world.cpu().data.numpy()
    error_3d = np.mean(np.linalg.norm(bone_pos_3d_GT - P_world, axis=0))
    pose_client.error_3d.append(error_3d)
    if (plot_loc != 0):
        #superimpose_on_image(bone_2d.data.numpy(), plot_loc, client.linecount, bone_connections, photo_loc, custom_name="projected_res_", scale = -1, projection=check.data.numpy())
        plot_drone_and_human(bone_pos_3d_GT, P_world, plot_loc, airsim_client.linecount, bone_connections, error_3d)
        if (airsim_client.linecount >1):
            plot_optimization_losses(pltpts, plot_loc, airsim_client.linecount, loss_dict)

    positions = form_positions_dict(angle, drone_pos_vec, P_world[:,0])
    cov = transform_cov_matrix(R_drone.cpu().data.numpy(), measurement_cov_)
    f_output_str = '\t'+str(unreal_positions[HUMAN_POS_IND, 0]) +'\t'+str(unreal_positions[HUMAN_POS_IND, 1])+'\t'+str(unreal_positions[HUMAN_POS_IND, 2])+'\t'+str(angle[0])+'\t'+str(angle[1])+'\t'+str(angle[2])+'\t'+str(drone_pos_vec.x_val)+'\t'+str(drone_pos_vec.y_val)+'\t'+str(drone_pos_vec.z_val)
    plot_end = {"est": P_world, "GT": bone_pos_3d_GT, "drone": C_drone, "eval_time": func_eval_time, "f_string": f_output_str}
    pose_client.append_res(plot_end)

    return positions, unreal_positions, cov

def determine_3d_positions_backprojection(airsim_client, pose_client, measurement_cov_, plot_loc = 0, photo_loc = 0, quiet = False):
    unreal_positions, bone_pos_3d_GT, drone_pos_vec, angle = airsim_client.getSynchronizedData()
    bone_connections, joint_names, _, bone_pos_3d_GT = model_settings(pose_client.model, bone_pos_3d_GT)

    bone_2d, _, _, _ = determine_2d_positions(pose_client.modes["mode_2d"], pose_client.cropping_tool, False, False, unreal_positions, bone_pos_3d_GT, photo_loc, -1)

    R_drone = euler_to_rotation_matrix(unreal_positions[DRONE_ORIENTATION_IND, 0], unreal_positions[DRONE_ORIENTATION_IND, 1], unreal_positions[DRONE_ORIENTATION_IND, 2])
    C_drone = unreal_positions[DRONE_POS_IND, :]
    C_drone = C_drone[:, np.newaxis]
    #Uncomment for AirSim Metrics
    #R_drone = euler_to_rotation_matrix(angle[1], angle[0], angle[2])
    #C_drone = np.array([[drone_pos_vec.x_val],[drone_pos_vec.y_val],[drone_pos_vec.z_val]])

    P_world = take_bone_backprojection(bone_2d, R_drone, C_drone, joint_names)
    error_3d = np.linalg.norm(bone_pos_3d_GT - P_world, )
    pose_client.error_3d.append(error_3d)

    if (plot_loc != 0):
        check, _, _ = take_bone_projection(P_world, R_drone, C_drone)
        superimpose_on_image([check], plot_loc, airsim_client.linecount, bone_connections, photo_loc)
        plot_drone_and_human(bone_pos_3d_GT, P_world, plot_loc, airsim_client.linecount, bone_connections, error_3d)

    cov = transform_cov_matrix(R_drone, measurement_cov_)

    positions = form_positions_dict(angle, drone_pos_vec, P_world[:,0])
    f_output_str = '\t'+str(unreal_positions[HUMAN_POS_IND, 0]) +'\t'+str(unreal_positions[HUMAN_POS_IND, 1])+'\t'+str(unreal_positions[HUMAN_POS_IND, 2])+'\t'+str(angle[0])+'\t'+str(angle[1])+'\t'+str(angle[2])+'\t'+str(drone_pos_vec.x_val)+'\t'+str(drone_pos_vec.y_val)+'\t'+str(drone_pos_vec.z_val)
    plot_end = {"est": P_world, "GT": bone_pos_3d_GT, "drone": C_drone, "eval_time": 0, "f_string": f_output_str}
    pose_client.append_res(plot_end)

    return positions, unreal_positions, cov

def determine_3d_positions_all_GT(airsim_client, pose_client, plot_loc, photo_loc, quiet):
    unreal_positions, bone_pos_3d_GT, drone_pos_vec, angle = airsim_client.getSynchronizedData()
    bone_connections, joint_names, num_of_joints, bone_pos_3d_GT = model_settings(pose_client.model, bone_pos_3d_GT)
    R_drone = Variable(euler_to_rotation_matrix(unreal_positions[DRONE_ORIENTATION_IND, 0], unreal_positions[DRONE_ORIENTATION_IND, 1], unreal_positions[DRONE_ORIENTATION_IND, 2], returnTensor=True), requires_grad = False)
    C_drone = Variable(torch.FloatTensor([[unreal_positions[DRONE_POS_IND, 0]],[unreal_positions[DRONE_POS_IND, 1]],[unreal_positions[DRONE_POS_IND, 2]]]), requires_grad = False)
    
    if (pose_client.modes["mode_2d"] == 1):
        input_image = cv.imread(photo_loc)
        if (airsim_client.linecount == FRAME_START_OPTIMIZING+1):
            pose_client.cropping_tool.update_bbox_margin(1)

        cropped_image = pose_client.cropping_tool.crop(input_image)
        save_image(cropped_image, airsim_client.linecount, plot_loc, custom_name="cropped_img_")
        scales = pose_client.cropping_tool.scales
        bone_2d, heatmap_2d, heatmaps_scales, poses_scales = determine_2d_positions(pose_client.modes["mode_2d"], pose_client.cropping_tool, True, unreal_positions, bone_pos_3d_GT, cropped_image, scales)
        save_heatmaps(heatmap_2d.cpu().numpy(), airsim_client.linecount, plot_loc)
        save_heatmaps(heatmaps_scales.cpu().numpy(), airsim_client.linecount, plot_loc, custom_name = "heatmaps_scales_", scales=scales, poses=poses_scales.cpu().numpy(), bone_connections=bone_connections)

        bone_2d = pose_client.cropping_tool.uncrop_pose(bone_2d)
        pose_client.cropping_tool.update_bbox(numpy_to_tuples(bone_2d))

        #pose = torch.cat((torch.t(bone_2d), torch.ones(num_of_joints,1)), 1)

        #pose3d_lift, cropped_img, cropped_heatmap = liftnet_module.run(input_image, heatmap_2d.cpu().numpy(), pose)
        
        #pose3d_lift = pose3d_lift.view(num_of_joints+2,  -1).permute(1, 0)
        #pose3d_lift = rearrange_bones_to_mpi(pose3d_lift, is_torch = True)
        #pose3d_lift = camera_to_world(R_drone, C_drone, pose3d_lift.cpu(), is_torch = True)

        #pose3d_lift, _ = normalize_pose(pose3d_lift, joint_names, is_torch = True)
        #bone_pos_3d_GT, _ = normalize_pose(bone_pos_3d_GT, joint_names, is_torch = False)

        #if not quiet:
            #plot_drone_and_human(bone_pos_3d_GT, pose3d_lift.cpu().numpy(), plot_loc, client.linecount, bone_connections, custom_name="lift_res_", orientation = "z_up")


    elif (pose_client.modes["mode_2d"] == 0):
        bone_2d, heatmap_2d, _, _ = determine_2d_positions(pose_client.modes["mode_2d"], pose_client.cropping_tool, False, False, unreal_positions, bone_pos_3d_GT, photo_loc, 0)
        if not quiet:
            superimpose_on_image(bone_2d, plot_loc, airsim_client.linecount, bone_connections, photo_loc, custom_name="gt_", scale = 0)


    positions = form_positions_dict(angle, drone_pos_vec, unreal_positions[HUMAN_POS_IND,:])
    positions[HUMAN_POS_IND,2] = positions[HUMAN_POS_IND,2]
    positions[R_SHOULDER_IND,:] = unreal_positions[R_SHOULDER_IND,:]
    positions[L_SHOULDER_IND,:] = unreal_positions[L_SHOULDER_IND,:]

    f_output_str = '\t'+str(unreal_positions[HUMAN_POS_IND, 0]) +'\t'+str(unreal_positions[HUMAN_POS_IND, 1])+'\t'+str(unreal_positions[HUMAN_POS_IND, 2])+'\t'+str(angle[0])+'\t'+str(angle[1])+'\t'+str(angle[2])+'\t'+str(drone_pos_vec.x_val)+'\t'+str(drone_pos_vec.y_val)+'\t'+str(drone_pos_vec.z_val)+'\n'
    cov = 1e-20 * np.eye(3,3)
    plot_end = {"est": bone_pos_3d_GT, "GT": bone_pos_3d_GT, "drone": C_drone, "eval_time": 0, "f_string": f_output_str}
    pose_client.append_res(plot_end)

    return positions, unreal_positions, cov

def form_positions_dict(angle, drone_pos_vec, human_pos):
    positions = np.zeros([5, 3])
    positions[DRONE_POS_IND,:] = np.array([drone_pos_vec.x_val, drone_pos_vec.y_val, drone_pos_vec.z_val])
    positions[DRONE_ORIENTATION_IND,:] = np.array([angle[0], angle[1], angle[2]])
    positions[HUMAN_POS_IND,:] = human_pos
    positions[HUMAN_POS_IND,2] = positions[HUMAN_POS_IND,2]
    return positions

def switch_energy(value):
    pass