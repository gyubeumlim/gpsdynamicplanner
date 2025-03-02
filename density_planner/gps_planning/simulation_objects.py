import numpy as np
import torch
from gps_planning.utils import pos2gridpos, traj2grid, shift_array, \
    pred2grid, get_mesh_sample_points, sample_pdf, enlarge_grid, compute_gradient, make_path
from density_training.utils import load_nn, get_nn_prediction
from data_generation.utils import load_inputmap, load_outputmap
from plots.plot_functions import plot_ref, plot_grid, plot_traj
from matplotlib import cm
from matplotlib.colors import ListedColormap
from systems.sytem_CAR import Car
import matplotlib.pyplot as plt
from scipy.ndimage import gaussian_filter


class Environment:
    """
    Environment class which contains the occupation map
    """

    def __init__(self, objects, args, name="environment", timestep=0):
        # self.time = time
        self.device = args.device
        self.gpsgridvisualize = args.gpsgridvisualize
        self.custom_cost = args.gps_cost
        self.grid_size = args.grid_size
        self.grid = None
        self.gps_grid = None
        self.grid_size = args.grid_size
        self.current_timestep = timestep
        self.objects = objects
        self.name = name
        self.grid_enlarged = None
        self.grid_gradientX = None
        self.grid_gradientY = None
        self.gps_grid_gradientX = None
        self.gps_grid_gradientY = None
        self.args = args
        self.update_grid()


    def update_grid(self):
        """
        forward object occupancies to the current timestep and add all grids together
        """
        number_timesteps = self.current_timestep + 1
        self.grid = torch.zeros((self.grid_size[0], self.grid_size[1], number_timesteps), device=self.device)  # obstacle grid
        self.gps_grid = torch.zeros((self.grid_size[0], self.grid_size[1], number_timesteps), device=self.device)
        for obj in self.objects:
            if obj.isgps == True:
                if obj.gps_grid.shape[2] < number_timesteps:
                    obj.forward_occupancy(step_size=number_timesteps - obj.gps_grid.shape[2])

                self.add_gps_grid(obj.gps_grid)
                self.add_grid(obj.grid)
            else:
                if obj.grid.shape[2] < number_timesteps:
                    obj.forward_occupancy(step_size=number_timesteps - obj.grid.shape[2])
                self.add_grid(obj.grid)

        if self.custom_cost == False:
            self.grid = torch.clamp(self.grid + self.gps_grid, 0, 1)

            # plot the original environment
            if self.current_timestep == 100 and self.gpsgridvisualize == True:
                # curgrid = self.grid[:, :, 0].clone().detach()
                # curgpsgrid = self.gps_grid[:, :, 0].clone().detach()
                # self.grid = self.grid + self.gps_grid
                #
                # fig = plt.figure(1)
                # ax1 = fig.add_subplot(1, 3, 1)
                # ax1.imshow(np.rot90(curgrid.numpy(), 1))
                # ax1.set_title('Obstacle')
                # ax1.axis("off")
                #
                # ax2 = fig.add_subplot(1, 3, 2)
                # ax2.imshow(np.rot90(curgpsgrid.numpy(), 1))
                # ax2.set_title('GPS')
                # ax2.axis("off")
                #
                # ax3 = fig.add_subplot(1, 3, 3)
                # ax3.imshow(np.rot90(self.grid[:, :, self.current_timestep - 1].numpy(), 1))
                # ax3.set_title('Combined')
                # ax3.axis("off")
                #
                # plt.show()
                self.grid_visualizer()
        else:
            if self.current_timestep == 100 and self.gpsgridvisualize == True:
                self.grid_visualizer()

    def grid_visualizer(self):
        folder = make_path(self.args.path_plot_motion, "_gps_dynamic")

        # create colormap
        greys = cm.get_cmap('Greys')
        grey_col = greys(range(0, 256))
        greens = cm.get_cmap('Greens')
        green_col = greens(range(0, 256))
        blue = np.array([[0.212395, 0.359683, 0.55171, 1.]])
        # yellow = np.array([[0.993248, 0.906157, 0.143936, 1.      ]])
        colorarray = np.concatenate((grey_col[::2, :], green_col[::2, :], blue))
        cmap = ListedColormap(colorarray)

        grid_env_sc = 127 * self.gps_grid.clone().detach()

        for i in range(self.gps_grid.shape[2]):
            grid_all = torch.clamp(grid_env_sc[:, :, i], 0, 256)
            plot_grid(grid_all, self.args, name="iter%d" % i, cmap=cmap, show=False, save=True, folder=folder)

        if self.custom_cost == False:
            grid_env_sc = 127 * self.grid.clone().detach()
        else:
            grid_env_sc = torch.clamp(self.grid + self.gps_grid, 0, 1)
            grid_env_sc = 127 * grid_env_sc.clone().detach()

        folder = make_path(self.args.path_plot_motion, "gps+grid")

        for i in range(self.grid.shape[2]):
            grid_all = torch.clamp(grid_env_sc[:, :, i], 0, 256)
            plot_grid(grid_all, self.args, name="iter%d" % i, cmap=cmap, show=False, save=True, folder=folder)

    def add_gps_grid(self, grid):
        """
        add individual object grid to the overall occupancy grid
        """
        self.gps_grid = torch.clamp(self.gps_grid + grid[:, :, :self.current_timestep + 1], 0, 1)

    def add_grid(self, grid):
        """
        add individual object grid to the overall occupancy grid
        """
        self.grid = torch.clamp(self.grid + grid[:, :, :self.current_timestep + 1], 0, 1)

    def forward_occupancy(self, step_size=1):
        """
        increment current time step and update the grid
        """
        self.current_timestep += step_size
        self.update_grid()

    def enlarge_shape(self, table=None):
        """
        enlarge the shape of all obstacles and update the grid to do motion planning for a point
        """
        if table is None:
            table = [[0, 10, 25], [10, 30, 20], [30, 50, 10], [50, 101, 5]]
        grid_enlarged = self.grid.clone().detach()
        for elements in table:
            if elements[0] >= self.grid.shape[2]:
                continue
            timesteps = torch.arange(elements[0], min(elements[1], self.grid.shape[2]))
            grid_enlarged[:, :, timesteps] = enlarge_grid(self.grid[:, :, timesteps], elements[2])
        self.grid_enlarged = grid_enlarged

    def get_gradient(self):
        """
        compute the gradients of the occupation grid
        """
        if self.grid_gradientX is None:
            grid_gradientX, grid_gradientY = compute_gradient(self.grid, step=1)
            s = 5
            missingGrad = torch.logical_and(self.grid != 0, torch.logical_and(grid_gradientX == 0, grid_gradientY == 0))
            while torch.any(missingGrad):
                idx = missingGrad.nonzero(as_tuple=True)
                grid_gradientX_new, grid_gradientY_new = compute_gradient(self.grid, step=s)
                grid_gradientX[idx] += s * grid_gradientX_new[idx]
                grid_gradientY[idx] += s * grid_gradientY_new[idx]
                s += 10
                missingGrad = torch.logical_and(self.grid != 0, torch.logical_and(grid_gradientX == 0, grid_gradientY == 0))

        if self.gps_grid_gradientX is None:
            gps_grid_gradientX, gps_grid_gradientY = compute_gradient(self.gps_grid, step=1)
            s = 5
            missingGrad = torch.logical_and(self.gps_grid != 0,
                                            torch.logical_and(gps_grid_gradientX == 0, gps_grid_gradientY == 0))
            while torch.any(missingGrad):
                idx = missingGrad.nonzero(as_tuple=True)
                gps_grid_gradientX_new, gps_grid_gradientY_new = compute_gradient(self.gps_grid, step=s)
                gps_grid_gradientX[idx] += s * gps_grid_gradientX_new[idx]
                gps_grid_gradientY[idx] += s * gps_grid_gradientY_new[idx]
                s += 10
                missingGrad = torch.logical_and(self.grid != 0,
                                                torch.logical_and(gps_grid_gradientX == 0, gps_grid_gradientY == 0))

            self.grid_gradientX = grid_gradientX
            self.grid_gradientY = grid_gradientY
            self.gps_grid_gradientX = gps_grid_gradientX
            self.gps_grid_gradientY = gps_grid_gradientY

class StaticObstacle:
    """
    Stationary object with occupancy map
    """

    def __init__(self, args, coord, name="staticObstacle", timestep=0, isgps = True):
        """
        initialize obstacle

        :param args:    settings
        :param coord:   list of position, certainty and spread of the initial obstacle shape
        :param name:    name
        :param timestep:duration the obstacle exists
        """
        self.device = args.device
        self.grid_size = args.grid_size
        self.current_timestep = 0
        self.name = name
        self.grid = torch.zeros((self.grid_size[0], self.grid_size[1], timestep + 1), device=self.device)
        self.grid.to(self.device)
        self.gps_grid = torch.zeros((self.grid_size[0], self.grid_size[1], timestep + 1), device=self.device)
        self.gps_grid.to(self.device)
        self.bounds = [None for _ in range(timestep + 1)]
        self.occupancies = []
        self.base_sigma = 7

        self.basenum = int(name[-1:])
        if self.basenum == 0 or self.basenum == 2:
            self.base_sigma = 7

        self.isgps = isgps
        self.add_occupancy(args, pos=coord[0:4], certainty=coord[4], spread=coord[5])

    def add_occupancy(self, args, pos, certainty=1, spread=1, pdf_form='square'):
        """
        add shape to the obstacle

        :param args:        setting
        :param pos:         position
        :param certainty:   occupation probability
        :param spread:      spread of the obstacle in occupation map
        :param pdf_form:    shape
        """
        grid_pos_x, grid_pos_y = pos2gridpos(args, pos[:2], pos[2:])
        normalise = False
        if certainty is None:
            certainty = 1
            normalise = True
        if pdf_form == 'square':
            for i in range(int(spread)):
                min_x = max(grid_pos_x[0] - i, 0)
                max_x = min(grid_pos_x[1] + i + 1, self.grid.shape[0])
                min_y = max(grid_pos_y[0] - i, 0)
                max_y = min(grid_pos_y[1] + i + 1, self.grid.shape[1])
                self.grid[min_x:max_x, min_y:max_y, self.current_timestep] += certainty / spread
                limits = torch.tensor([grid_pos_x[0] - i, grid_pos_x[1] + i, grid_pos_y[0] - i, grid_pos_y[1] + i])
            if self.bounds[self.current_timestep] is None:
                self.bounds[self.current_timestep] = limits
            else:
                self.bounds[self.current_timestep] = torch.cat((self.bounds[self.current_timestep],
                                                                limits), dim=0)
        else:
            raise (NotImplementedError)

        if normalise:
            self.grid[:, :, self.current_timestep] /= self.grid[:, :, self.current_timestep].sum()

        # implement gps by adding gaussian grid, update grid
        samplegrid = np.array(self.grid[:, :, self.current_timestep], copy=True)
        sigmaval = self.base_sigma
        gupdatedgrid = gaussian_filter(samplegrid * 1.5, sigma=sigmaval)
        gpsmap = torch.FloatTensor(gupdatedgrid)
        gpsmap = torch.clamp(gpsmap, 0, 1)

        # gaussian dist for grid
        samplegrid = np.array(self.grid[:, :, self.current_timestep], copy=True)
        gupdatedgrid = gaussian_filter(samplegrid, sigma=sigmaval)
        new_grid = torch.FloatTensor(gupdatedgrid)
        new_grid = torch.clamp(new_grid, 0, 1)

        self.grid[:, :, self.current_timestep] = new_grid
        self.gps_grid[:, :, self.current_timestep] = gpsmap

    def forward_occupancy(self, step_size=1):
        """
        forward the obstacle occupancies in time

        :param step_size: number of time steps the obstacle should be forwarded
        """
        self.grid = torch.cat((self.grid, torch.repeat_interleave(self.grid[:, :, [self.current_timestep]], step_size,
                                                                  dim=2)), dim=2)

        self.gps_grid = torch.cat((self.gps_grid, torch.repeat_interleave(self.gps_grid[:, :, [self.current_timestep]], step_size,
                                                                  dim=2)), dim=2)
        self.bounds += [self.bounds[self.current_timestep]] * step_size
        self.current_timestep += step_size

    def enlarge_shape(self, wide):
        pass


class DynamicObstacle(StaticObstacle):
    """
    Dynamic object with occupancy map, with gps
    """

    def __init__(self, args, coord, name="dynamicObstacle", timestep=0, velocity_x=0, velocity_y=0, gps_growthrate=0, isgps = True):
        """
        initialize object

        :param args:        settings
        :param coord:       list of position, certainty and spread of the obstacle
        :param name:        name
        :param timestep:    duration the obstacle exists
        :param velocity_x:  velocity in x direction (number of grid cells the obstacle moves along the x axis during
                                each time step)
        :param velocity_y:  velocity in y direction
        """

        self.velocity_x = velocity_x
        self.velocity_y = velocity_y
        self.gps_growthrate = gps_growthrate

        #testing parameter
        self.growshrink = False
        self.x_offset = 1.1
        self.onesidegrow = True
        self.targetbase = 0
        self.movingbase = 2

        super().__init__(args, coord, name=name, timestep=timestep)

    def add_occupancy(self, args, pos, certainty=1, spread=1, pdf_form='square'):
        """
        add shape to the obstacle

        :param args:        setting
        :param pos:         position
        :param certainty:   occupation probability
        :param spread:      spread of the obstacle in occupation map
        :param pdf_form:    shape
        """
        grid_pos_x, grid_pos_y = pos2gridpos(args, pos[:2], pos[2:])
        normalise = False
        if certainty is None:
            certainty = 1
            normalise = True
        if pdf_form == 'square':
            for i in range(int(spread)):
                min_x = max(grid_pos_x[0] - i, 0)
                max_x = min(grid_pos_x[1] + i + 1, self.grid.shape[0])
                min_y = max(grid_pos_y[0] - i, 0)
                max_y = min(grid_pos_y[1] + i + 1, self.grid.shape[1])
                self.grid[min_x:max_x, min_y:max_y, self.current_timestep] += certainty / spread
                limits = torch.tensor([grid_pos_x[0] - i, grid_pos_x[1] + i, grid_pos_y[0] - i, grid_pos_y[1] + i])
            if self.bounds[self.current_timestep] is None:
                self.bounds[self.current_timestep] = limits
            else:
                self.bounds[self.current_timestep] = torch.cat((self.bounds[self.current_timestep],
                                                                limits), dim=0)
        else:
            raise (NotImplementedError)

        if normalise:
            self.grid[:, :, self.current_timestep] /= self.grid[:, :, self.current_timestep].sum()

        # implement gps by adding gaussian grid, update grid
        samplegrid = np.array(self.grid[:, :, self.current_timestep], copy=True)
        sigmaval = self.base_sigma
        gupdatedgrid = gaussian_filter(samplegrid * 1.5, sigma=sigmaval)
        gpsmap = torch.FloatTensor(gupdatedgrid)
        gpsmap = torch.clamp(gpsmap, 0, 1)

        # gaussian dist for grid
        samplegrid = np.array(self.grid[:, :, self.current_timestep], copy=True)
        gupdatedgrid = gaussian_filter(samplegrid, sigma=sigmaval)
        new_grid = torch.FloatTensor(gupdatedgrid)
        new_grid = torch.clamp(new_grid, 0, 1)

        self.grid[:, :, self.current_timestep] = new_grid

        if self.onesidegrow and self.basenum == self.targetbase:
            gpsmap = self.gps_shift_array(iter=-1, grid=samplegrid, step_x=self.velocity_x, step_y=0, fill=0)
        if self.onesidegrow and self.basenum == self.movingbase:
            gpsmap = self.gps_shift_array(iter=-1, grid=samplegrid, step_x=self.velocity_x, step_y=0, fill=0)

        self.gps_grid[:, :, self.current_timestep] = gpsmap

        # plt.imshow(np.rot90(self.gps_grid[:, :, self.current_timestep].numpy(), 1))
        # plt.show()

    def forward_occupancy(self, step_size=1):
        """
        forward the obstacle occupancies in time

        :param step_size: number of time steps the obstacle should be forwarded
        """

        #forward the grid
        self.grid = torch.cat((self.grid, torch.repeat_interleave(self.grid[:, :, [self.current_timestep]], step_size,
                                                                  dim=2)), dim=2)
        # move the gps map
        sum = 1
        self.gps_grid = torch.cat((self.gps_grid, torch.zeros((self.grid_size[0], self.grid_size[1], step_size) , device=self.device)), dim=2)
        self.gps_grid.to(self.device)
        for i in range(step_size):
            self.gps_grid[:, :, self.current_timestep + 1 + i] = self.gps_shift_array(i,self.grid[:, :, self.current_timestep + i],
                                                                         self.velocity_x, self.velocity_y)
            self.bounds.append(self.bounds[self.current_timestep + i] +
                               torch.tensor([self.velocity_x, self.velocity_x, self.velocity_y, self.velocity_y]))

        self.current_timestep += step_size

    def gps_shift_array(self,iter, grid, step_x=0, step_y=0, fill=0):
        """
        shift array or tensor

        :param grid:    array/ tensor which should be shifted
        :param step_x:  shift in x direction
        :param step_y:  shift in y direction
        :param fill:    value which should be used to fill the new cells
        :return: shifted array / tensor
        """

        # implement gps by adding gaussian grid, update grid
        samplegrid = np.array(grid, copy=True)
        if self.gps_growthrate == 0:
            sigmaval = self.base_sigma
        else:
            sigmaval = self.gps_growthrate * iter + self.base_sigma

            if self.growshrink == True:
                sigmaval = self.gps_growthrate * iter*2 + self.base_sigma

                if iter > 50:
                    iter = 100 - iter
                    sigmaval = self.gps_growthrate * iter*2 + self.base_sigma

        gupdatedgrid = gaussian_filter(samplegrid * 1.5, sigma=sigmaval)
        gpsmap = torch.FloatTensor(gupdatedgrid)

        gpsmap = torch.clamp(gpsmap, 0, 1)

        if self.onesidegrow and self.basenum == self.targetbase:
            step_x = int(self.x_offset*10)
        elif self.onesidegrow and self.basenum == self.movingbase:
            step_x = int(step_x * iter-self.x_offset*10)
        else:
            step_x = int(step_x * iter)

        step_y = int(step_y * iter)

        result = torch.zeros_like(gpsmap, device=self.device)

        if step_x > 0:
            result[:step_x, :] = fill
            result[step_x:, :] = gpsmap[:-step_x, :]
        elif step_x < 0:
            result[step_x:, :] = fill
            result[:step_x, :] = gpsmap[-step_x:, :]
        else:
            result = gpsmap

        result_new = torch.zeros_like(gpsmap, device=self.device)
        if step_y > 0:
            result_new[:, :step_y] = fill
            result_new[:, step_y:] = result[:, :-step_y]
        elif step_y < 0:
            result_new[:, step_y:] = fill
            result_new[:, :step_y] = result[:, -step_y:]
        else:
            result_new = result

        result_new.to(self.device)
        return result_new


class EgoVehicle:
    """
    class for the ego vehicle which contains all information for the motion planning task
    """

    def __init__(self, xref0, xrefN, env, args, pdf0=None, name="egoVehicle", video=False):
        self.device = env.device
        self.xref0 = xref0
        self.xrefN = xrefN
        self.system = Car()
        self.name = name
        self.env = env
        self.args = args
        self.video = video
        if pdf0 is None:
            pdf0 = sample_pdf(self.system, args.mp_gaussians)
        self.initialize_predictor(pdf0)
        self.env.get_gradient()

    def initialize_predictor(self, pdf0):
        """
        sample initial states from the initial density distribution

        :param pdf0:    initial density distribution
        """
        self.model = self.load_predictor(self.system.DIM_X)
        if self.args.mp_sampling == 'random':
            xe0 = torch.rand(self.args.mp_sample_size, self.system.DIM_X, 1) * (
                    self.system.XE0_MAX - self.system.XE0_MIN) + self.system.XE0_MIN
        else:
            _, xe0 = get_mesh_sample_points(self.system, self.args)
            xe0 = xe0.unsqueeze(-1)
        rho0 = pdf0(xe0)
        mask = rho0 > 0
        self.xe0 = xe0[mask, :, :]
        self.rho0 = (rho0[mask] / rho0.sum()).reshape(-1, 1, 1)

    def load_predictor(self, dim_x):
        """
        load the density NN

        :param dim_x: dimensionaliy of the state
        :return: model of the NN
        """
        _, num_inputs = load_inputmap(dim_x, self.args)
        _, num_outputs = load_outputmap(dim_x)
        model, _ = load_nn(num_inputs, num_outputs, self.args, load_pretrained=True)
        model.eval()
        return model

    def predict_density(self, up, xref_traj, use_nn=True, xe0=None, rho0=None, compute_density=True):
        """
        predict the state and denisty trajectories

        :param up:              parameters of the reference trajectory
        :param xref_traj:       reference trajectory
        :param use_nn:          True if density NN is used for the predictions, otherwise LE
        :param xe0:             initial deviations of the reference trajectory
        :param rho0:            initial density values
        :param compute_density: True if density should be computed, otherwise just computation of the state trajectories
        :return: state and density trajectories
        """
        if xe0 is None:
            xe0 = self.xe0
        if rho0 is None:
            rho0 = self.rho0
            assert rho0.shape[0] == xe0.shape[0]

        if self.args.input_type == "discr10" and up.shape[2] != 10:
            N_sim = up.shape[2] * (self.args.N_sim_max // 10) + 1
            up = torch.cat((up, torch.zeros(up.shape[0], up.shape[1], 10 - up.shape[2])), dim=2, device=self.device)
        elif self.args.input_type == "discr5" and up.shape[2] != 5:
            N_sim = up.shape[2] * (self.args.N_sim_max // 5) + 1
            up = torch.cat((up, torch.zeros(up.shape[0], up.shape[1], 5 - up.shape[2])), dim=2, device=self.device)
        else:
            N_sim = self.args.N_sim

        if use_nn: # approximate with density NN
            xe_traj = torch.zeros(xe0.shape[0], xref_traj.shape[1], xref_traj.shape[2], device=self.device)
            rho_log_unnorm = torch.zeros(xe0.shape[0], 1, xref_traj.shape[2], device=self.device)
            t_vec = torch.arange(0, self.args.dt_sim * N_sim - 0.001, self.args.dt_sim * self.args.factor_pred)

            # 2. predict x(t) and rho(t) for times t
            for idx, t in enumerate(t_vec):
                xe_traj[:,:, [idx]], rho_log_unnorm[:, :, [idx]] = get_nn_prediction(self.model, xe0[:, :, 0], self.xref0[0, :, 0], t, up, self.args)
        else: # use LE
            uref_traj, _ = self.system.sample_uref_traj(self.args, up=up)
            xref_traj_long = self.system.compute_xref_traj(self.xref0, uref_traj, self.args)
            xe_traj_long, rho_log_unnorm = self.system.compute_density(xe0, xref_traj_long, uref_traj, self.args.dt_sim,
                                                   cutting=False, log_density=True, compute_density=True)
            xe_traj = xe_traj_long[:, :, ::self.args.factor_pred]

        if compute_density:
            rho_max, _ = rho_log_unnorm.max(dim=0)
            rho_unnorm = torch.exp(rho_log_unnorm - rho_max.unsqueeze(0)) * rho0.reshape(-1, 1, 1)
            rho_traj = rho_unnorm / rho_unnorm.sum(dim=0).unsqueeze(0)
            rho_traj = rho_traj #+ rho0.reshape(-1, 1, 1)
        else:
            rho_traj = None

        x_traj = xe_traj + xref_traj
        return x_traj, rho_traj

    def visualize_xref(self, xref_traj, uref_traj=None, show=True, save=False, include_date=True,
                       name='Reference Trajectory', folder=None, x_traj=None):
        """
        plot the reference trajectory in the occupation map of the environment

        :param xref_traj:   reference state trajectory
        :param uref_traj:   reference input trajectory
        :param show:        True if plot should be shown
        :param save:        True if plot should be saved
        :param include_date:True if filename includes the date
        :param name:        name for the plot
        :param folder:      folder where the plot should be saved
        """

        if uref_traj is not None:
            plot_ref(xref_traj, uref_traj, 'Reference Trajectory', self.args, self.system, t=self.t_vec,
                     include_date=True)

        ego_dict = {"grid": torch.clamp(self.env.grid + self.env.gps_grid, 0, 1),
                    "start": self.xref0,
                    "goal": self.xrefN,
                    "args": self.args}
        if x_traj is not None:
            mp_methods = ["sys", "ref"]
            mp_results = {"sys": {"x_traj": [x_traj]}, "ref": {"x_traj": [xref_traj]}}
        else:
            mp_methods = ["ref"]
            mp_results = {"ref": {"x_traj": [xref_traj]}}

        plot_traj(ego_dict, mp_results, mp_methods, self.args, folder=folder, traj_idx=0, animate=False,
                  include_density=False, name=name)

    def animate_traj(self, folder, xref_traj, x_traj=None, rho_traj=None):
        """
        plot the density and the states in the occupation map for each point in time

        :param folder:      name of the folder for saving the plots
        :param xref_traj:   reference state trajectory
        :param x_traj:      state trajectories
        :param rho_traj:    density trajectories
        """

        if x_traj is None:
            x_traj = xref_traj
        if rho_traj is None:
            rho_traj = torch.ones(x_traj.shape[0], 1, x_traj.shape[2]) / x_traj.shape[0]  # assume equal density

        # create colormap
        greys = cm.get_cmap('Greys')
        grey_col = greys(range(0, 256))
        greens = cm.get_cmap('Greens')
        green_col = greens(range(0, 256))
        blue = np.array([[0.212395, 0.359683, 0.55171, 1.]])
        # yellow = np.array([[0.993248, 0.906157, 0.143936, 1.      ]])
        colorarray = np.concatenate((grey_col[::2, :], green_col[::2, :], blue))
        cmap = ListedColormap(colorarray)

        if self.env.custom_cost == False:
            grid_env_sc = 127 * self.env.grid.clone().detach()
        else:
            grid_env_sc = torch.clamp(self.env.grid + self.env.gps_grid, 0, 1)
            grid_env_sc = 127 * grid_env_sc.clone().detach()

        for i in range(xref_traj.shape[2]):
            with torch.no_grad():
                # 3. compute marginalized density grid
                grid_pred = pred2grid(x_traj[:, :, [i]], rho_traj[:, :, [i]], self.args, return_gridpos=False)

            grid_pred_sc = 127 * torch.clamp(grid_pred/grid_pred.max(), 0, 1)
            grid_pred_sc[grid_pred_sc != 0] += 128
            grid_traj = traj2grid(xref_traj[:, :, :i + 1], self.args)
            grid_traj[grid_traj != 0] = 256
            grid_all = torch.clamp(grid_env_sc[:, :, i] + grid_traj + grid_pred_sc[:, :, 0], 0, 256)
            plot_grid(grid_all, self.args, name="iter%d" % i, cmap=cmap, show=False, save=True, folder=folder)

    def animate_trajs(self, folder, xref_traj, x_traj=None, rho_traj=None):
        """
        plot the density and the states in the occupation map for each point in time

        :param folder:      name of the folder for saving the plots
        :param xref_traj:   reference state trajectory
        :param x_traj:      state trajectories
        :param rho_traj:    density trajectories
        """

        if x_traj is None:
            x_traj = xref_traj
        if rho_traj is None:
            rho_traj = torch.ones(x_traj.shape[0], 1, x_traj.shape[2]) / x_traj.shape[0]  # assume equal density

        # create colormap
        greys = cm.get_cmap('Greys')
        grey_col = greys(range(0, 256))
        greens = cm.get_cmap('Greens')
        green_col = greens(range(0, 256))
        blue = np.array([[0.212395, 0.359683, 0.55171, 1.]])
        # yellow = np.array([[0.993248, 0.906157, 0.143936, 1.      ]])
        colorarray = np.concatenate((grey_col[::2, :], green_col[::2, :], blue))
        cmap = ListedColormap(colorarray)

        if self.env.custom_cost == False:
            grid_env_sc = 127 * self.env.grid.clone().detach()
        else:
            grid_env_sc = torch.clamp(self.env.grid + self.env.gps_grid, 0, 1)
            grid_env_sc = 127 * grid_env_sc.clone().detach()

        for i in range(xref_traj.shape[2]):
            with torch.no_grad():
                # 3. compute marginalized density grid
                grid_pred = pred2grid(x_traj[:, :, [i]], rho_traj[:, :, [i]], self.args, return_gridpos=False)

            grid_pred_sc = 127 * torch.clamp(grid_pred/grid_pred.max(), 0, 1)
            grid_pred_sc[grid_pred_sc != 0] += 128
            grid_traj = traj2grid(xref_traj[:, :, :i + 1], self.args)
            grid_traj[grid_traj != 0] = 256
            grid_all = torch.clamp(grid_env_sc[:, :, i] + grid_traj + grid_pred_sc[:, :, 0], 0, 256)
            plot_grid(grid_all, self.args, name="iter%d" % i, cmap=cmap, show=False, save=True, folder=folder)

    def set_start_grid(self):
        self.grid = pred2grid(self.xref0 + self.xe0, self.rho0, self.args)