#!/usr/bin/env python3

import enum
import rospy
import rospkg
import tf
import dynamic_reconfigure.client

import numpy as np
import pinocchio

from multicopter_mpc_viz import MpcController
from multicopter_mpc_viz import WholeBodyStatePublisher
from multicopter_mpc_viz import WholeBodyTrajectoryPublisher

from sensor_msgs.msg import JointState
from geometry_msgs.msg import Pose
from mav_msgs.msg import Actuators
from multicopter_mpc_msgs.msg import SolverPerformance


class MpcControllerNode():
    def __init__(self):
        rospy.init_node('controller', anonymous=True)

        self.rate = rospy.Rate(500)

        self.load_params()

        self.mpcController = MpcController(self.trajectoryPath, self.trajectoryDt, self.trajectorySolver, self.mpcPath,
                                           self.mpcType)
        namespace = rospy.get_namespace()
        with open(self.mpcController.mpcController.robot_model_path, "r") as urdf_file:
            urdf_string = urdf_file.read()
        rospy.set_param(namespace + "robot_description", urdf_string)

        if self.bag_path == "":
            self.mpcController.compute_mpc_trajectory()

            self.xs = self.mpcController.xs
            self.us = self.mpcController.us
            self.us.append(self.us[-1])
            self.idxTrj = 0

            self.br = tf.TransformBroadcaster()
            self.qs, self.vs, self.ts = [], [], []
            nq = self.mpcController.mpcController.robot_model.nq
            for x in self.xs:
                self.qs.append(x[:nq])
                self.vs.append(x[nq:])
                self.ts.append(0.1)
        else:
            self.r_model = self.mpcController.mpcController.robot_model
            self.r_data = self.r_model.createData()
            self.q = np.zeros(self.r_model.nq)
            self.v = np.zeros(self.r_model.nv)
            self.thrusts = np.zeros(self.mpcController.mpcController.platform_params.n_rotors)
            self.initial_state = np.zeros(self.r_model.nq + self.r_model.nv)

        self.set_publishers_subscribers()

    def load_params(self):
        rospack = rospkg.RosPack()

        self.bag_path = rospy.get_param(rospy.get_namespace() + "/bag_path", "")

        self.trajectoryPath = rospy.get_param(
            rospy.get_namespace() + "/trajectory_path",
            rospack.get_path('multicopter_mpc_yaml') + '/trajectories/quad_hover.yaml')
        self.mpcPath = rospy.get_param(rospy.get_namespace() + "/mpc_path",
                                       rospack.get_path('multicopter_mpc_yaml') + '/mpc/mpc.yaml')
        self.trajectoryDt = rospy.get_param(rospy.get_namespace() + "/trajectory_dt", 10)
        self.trajectorySolver = rospy.get_param(rospy.get_namespace() + "/trajectory_solver", "SolverSbFDDP")
        self.mpcType = rospy.get_param(rospy.get_namespace() + "/mpc_type", "carrot")
        self.horizon_enabled = rospy.get_param(rospy.get_namespace() + "/horizon_enable", False)

    def set_publishers_subscribers(self):
        self.statePub = WholeBodyStatePublisher('whole_body_state',
                                                self.mpcController.mpcController.robot_model,
                                                self.mpcController.mpcController.platform_params,
                                                frame_id="world")

        if self.bag_path == "":
            self.trajectoryPub = WholeBodyTrajectoryPublisher('whole_body_trajectory',
                                                              self.mpcController.mpcController.robot_model,
                                                              self.mpcController.mpcController.platform_params,
                                                              frame_id="world")
            self.trajectoryTimer = rospy.Timer(rospy.Duration(2), self.callbackTrajectoryTimer)
            self.dynRecClient = dynamic_reconfigure.client.Client(
                "/" + rospy.get_param(rospy.get_namespace() + "/dynamic_reconfigure_client"),
                config_callback=self.callbackTrajectoryIdx)
            self.stateTimer = rospy.Timer(rospy.Duration(0.002), self.callbackStateTimer)
        else:
            self.ground_truth_sub = rospy.Subscriber("/hexacopter370/ground_truth/pose", Pose,
                                                     self.callback_ground_truth)
            self.joint_state_sub = rospy.Subscriber("/hexacopter370/joint_states", JointState,
                                                    self.callback_joint_states)
            self.joint_state_sub = rospy.Subscriber("/hexacopter370/motor_speed", Actuators, self.callback_actuators)
        if self.horizon_enabled:
            self.partialTrajectoryPub = WholeBodyTrajectoryPublisher('whole_body_partial_trajectory',
                                                                     self.mpcController.mpcController.robot_model,
                                                                     self.mpcController.mpcController.platform_params,
                                                                     frame_id="world")
            if self.bag_path != "":
                self.solver_performance_sub = rospy.Subscriber("/hexacopter370/solver_performance", SolverPerformance,
                                                               self.callback_solver_performance)

    def callbackTrajectoryTimer(self, timer):
        self.trajectoryPub.publish(self.ts, self.qs, self.vs)

    def callbackStateTimer(self, timer):
        x = self.xs[self.idxTrj]
        nq = self.mpcController.mpcController.robot_model.nq
        nRotors = self.mpcController.mpcController.platform_params.n_rotors
        self.statePub.publish(0.123, x[:nq], x[nq:], self.us[self.idxTrj][:nRotors], self.us[self.idxTrj][nRotors:])

        qs, vs, ts = [], [], []
        for x in self.mpcController.xss[self.idxTrj]:
            qs.append(x[:nq])
            vs.append(x[nq:])
            ts.append(0.1)
        if self.horizon_enabled:
            self.partialTrajectoryPub.publish(ts[0::2], qs[0::2], vs[0::2])

    def callbackTrajectoryIdx(self, config):
        self.trajectory_percentage = config.trajectory_percentage
        self.idxTrj = int((len(self.xs) - 1) * self.trajectory_percentage / 100)

    def callback_ground_truth(self, data):
        self.q[0] = data.position.x
        self.q[1] = data.position.y
        self.q[2] = data.position.z
        self.q[3] = data.orientation.x
        self.q[4] = data.orientation.y
        self.q[5] = data.orientation.z
        self.q[6] = data.orientation.w

        pinocchio.forwardKinematics(self.r_model, self.r_data, self.q)
        pinocchio.updateFramePlacement(self.r_model, self.r_data, self.r_model.getFrameId("flying_arm_3__gripper"))

        Mball = self.r_data.oMf[self.r_model.getFrameId("flying_arm_3__gripper")]

        self.statePub.publish(0.123, self.q, self.v, self.thrusts, np.zeros(3))
        # self.state_pub2.publish(0.123, self.q2, self.v2, np.zeros(6), np.array([]))

    def callback_joint_states(self, data):
        if "flying_arm_3" in data.name[0] and hasattr(data, "position"):
            for i in range(len(data.position)):
                self.q[7 + i] = data.position[i]

    def callback_solver_performance(self, data):
        qs, vs, ts = [], [], []
        for msg_floating in data.floating_base_trajectory:
            q = np.zeros(self.r_model.nq)
            v = np.zeros(self.r_model.nv)
            q[6] = 1
            q[0] = msg_floating.pose.position.x
            q[1] = msg_floating.pose.position.y
            q[2] = msg_floating.pose.position.z
            qs.append(q)
            vs.append(v)
            ts.append(0.1)
        self.partialTrajectoryPub.publish(ts[0::2], qs[0::2], vs[0::2])

    def callback_actuators(self, data):
        for idx, ang_speed in enumerate(data.angular_velocities):
            self.thrusts[idx] = ang_speed**2 * self.mpcController.mpcController.platform_params.cf


if __name__ == '__main__':
    mpc_node = MpcControllerNode()

    try:
        while not rospy.is_shutdown():
            mpc_node.rate.sleep()

    except rospy.ROSInterruptException:
        pass
