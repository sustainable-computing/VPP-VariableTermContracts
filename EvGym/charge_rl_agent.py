import numpy as np
import cvxpy as cp # type: ignore
import pandas as pd
from . import config
from typing import TYPE_CHECKING, Any
import numpy.typing as npt

np.set_printoptions(linewidth=np.nan) # type: ignore

import torch
import torch.nn as nn
import torch.optim as optim
from torch.distributions.normal import Normal
from torch.utils.tensorboard import SummaryWriter

def layer_init(layer, std=np.sqrt(2), bias_const =0.0):
    torch.nn.init.orthogonal_(layer.weight, std)
    torch.nn.init.constant_(layer.bias, bias_const)
    return layer


class agentPPO(nn.Module):
    def __init__(self, envs, df_price, device, n = 24, max_cars: int = config.max_cars):
        super().__init__()
        self.critic = nn.Sequential(
                layer_init(nn.Linear(envs["single_observation_space"], 64)),
                nn.Tanh(),
                layer_init(nn.Linear(64, 64)),
                nn.Tanh(),
                layer_init(nn.Linear(64,1), std=1.0)
                )
        self.actor_mean = nn.Sequential(
                layer_init(nn.Linear(envs["single_observation_space"], 64)),
                nn.Tanh(),
                layer_init(nn.Linear(64,64)),
                nn.Tanh(),
                layer_init(nn.Linear(64, envs["single_action_space"]), std=0.01),
                )
        self.actor_logstd = nn.Parameter(torch.zeros(1, envs["single_action_space"]))


        # Ev parameters
        self.max_cars = max_cars
        self.df_price = df_price
        self.device = device
        self.envs = envs
        self.n = n
        

    def get_value(self, x):
        return self.critic(x)

    def _get_action_and_value(self, x, action=None):
        print(x.type())
        action_mean = self.actor_mean(x)
        action_logstd = self.actor_logstd.expand_as(action_mean)
        action_std = torch.exp(action_logstd)
        probs = Normal(action_mean, action_std)
        if action is None:
            action = probs.sample()
        return action, probs.log_prob(action).sum(1), probs.entropy().sum(1), self.critic(x)

    def _get_prediction(self, t):
        l_idx_t0 = self.df_price.index[self.df_price["ts"]== t].to_list()
        assert len(l_idx_t0) == 1, "Timestep for prediction not unique or non existent"
        idx_t0 = l_idx_t0[0]
        idx_tend = min(idx_t0+self.n, self.df_price.index.max()+1)
        pred_price = self.df_price.iloc[idx_t0:idx_tend]["price_im"].values
        return pred_price

    def construct_state(self, df_state, t):
        state_cars = df_state[["soc_t", "t_rem", "soc_dis", "t_dis"]].values.flatten().astype(np.float64)
        pred_price = self._get_prediction(t)
        hour = np.array([t % 24])
        np_x = np.concatenate((state_cars, pred_price, hour))
        x = torch.tensor(np_x).to(self.device).float().reshape(1, self.envs["single_observation_space"])
        self.t = t
        return x

    def _enforce_single_safety(self, action_t, x, t):
        state_vals = x.cpu().numpy()[:config.max_cars * 4].reshape(config.max_cars, 4)
        df_state = pd.DataFrame(data = state_vals, columns = ["soc_t", "t_rem", "soc_dis", "t_dis"])

        constraints = []
        AC =  cp.Variable((config.max_cars, n))
        AD = cp.Variable((config.max_cars, n))
        Y = cp.Variable((config.max_cars, n))
        SOC = cp.Variable((config.max_cars, n+1), nonneg=True)
        LAX = cp.Variable((num_cars), nonneg = True)

        constraints += [SOC  >= 0]
        constraints += [SOC <= config.FINAL_SOC]

        constraints += [SOC[:,0] ==  df_state["soc_t"]]
        
        #Charging limits
        constraints += [AC >= 0]
        constraints += [AC <= config.alpha_c / config.B]

        # Discharging limits
        constraints += [AD <= 0]
        constraints += [AD >= -config.alhpa_d / config.B]

        # Discharging ammount
        constraints += [ - cp.sum(AD, axis=1)/ config.eta_d <= df_state["soc_dis"]]

        for i, car in enumerate(df_state.itertuples()):
            if car.t_rem == 0:
                constraints += [AD[i,:] == 0]
                constraints += [AC[i,:] == 0]
            else:
                j_end = int(car.t_rem)
                if j_end < n:
                    constraints += [SOC[i, j_end:] == config.FINAL_SOC]

                j_dis = int(car.t_dis)
                if j_dis < n:
                    constraints += [AD[i, j_dis:] == 0]

                for j in range(n):
                    constraints += [SOC[i,j+1] == SOC[i,j] + AC[i,j] * config.eta_c + AD[i,j] / config.eta_d]

                if n > 0:
                    constraints += [LAX[i] == (car.t_rem - 1)  - ((config.FINAL_SOC - SOC[i, 1])*config.B) /
                            (config.alhpa_c * config.eta_c)]
        constraints += [LAX >= 0]
        constraints += [Y == AC + AD]

        objective = cp.Minimize(cp.sum_squares(Y - action_t))
        prob = cp.Problem(objective, constraints)
        prob.solve(solver=cp.MOSEK, verbose=False)

        Y_val = Y_value
        return action

    def _enforce_safety(self, action_t, x, t):
        n = self.n
        action_t_np = action_t.cpu().numpy()
        # Account for batches
        l_actions = []
        if action_t.ndim == 2:
            loops = action_t[0]
            for i in loops:
                action_i = self._enforce_single_safety(action_t[i], x[i], t)
                l_actions.append(action_i)
            action = np.array(l_action)
        else:
            action = self._enforce_single_safety(action_t, x, t)

        return action

    def get_action_and_value(self, x, action=None):
        #x = self.construct_state(df_state, t) # Gets performed twice (also in main), can streamline later
        action_t, logprob, entropy, value = self._get_action_and_value(x, action)
        action = self._enforce_safety(action_t, x, self.t )
        return action, logprob, entropy, value 





 
