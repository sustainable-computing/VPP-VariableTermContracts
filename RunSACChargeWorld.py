import argparse
import os
import random
import time
from distutils.util import strtobool

import gymnasium as gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from stable_baselines3.common.buffers import ReplayBuffer
from torch.utils.tensorboard import SummaryWriter

import pandas as pd
import numpy as np
from tabulate import tabulate
import pyfiglet # type: ignore
from colorama import init, Back, Fore
import argparse
from tqdm import tqdm
from icecream import ic # type: ignore

# User defined modules
from EvGym.charge_world import ChargeWorldEnv
from EvGym.charge_sac_agent import agentSAC_sagg, SoftQNetwork
from EvGym.charge_utils import parse_sac_args, print_welcome
from EvGym import config

# Contracts
from ContractDesign.time_contracts import general_contracts

torch.set_num_threads(8)

def runSim(args = None):
    if args is None:
        args = parse_sac_args()

    title = f"EvWorld-{args.agent}{args.desc}"

    # Random number generator, same throught the program for reproducibility
    rng = np.random.default_rng(args.seed)

    # Load datasets
    df_sessions = pd.read_csv(f"{config.data_path}{args.file_sessions}", parse_dates = ["starttime_parking", "endtime_parking"])
    ts_min = df_sessions["ts_arr"].min()
    ts_max = df_sessions["ts_dep"].max()

    df_price = pd.read_csv(f"{config.data_path}{args.file_price}", parse_dates=["date"])

    # Calculate contracts
    G, W, L_cont = general_contracts(thetas_i = config.thetas_i,
                                     thetas_j = config.thetas_j,
                                     c1 = config.c1,
                                     c2 = config.c2,
                                     kappa1 = config.kappa1,
                                     kappa2 = config.kappa2,
                                     alpha_d = config.alpha_d,
                                     psi = config.psi,
                                     IR = "fst", IC = "ort_l", monotonicity=False) # Tractable formulation

    L = np.round(L_cont,0) # L_cont →  L continuous
    contract_info = {"G": G, "W": W, "L": L}

    # Some agents are not allowed to discharge energy
    skip_contracts = True if args.agent in ["ASAP", "NoV2G"] else False

    # TRY NOT TO MODIFY: seeding
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    torch.backends.cudnn.deterministic = args.torch_deterministic

    device = torch.device("cuda" if torch.cuda.is_available() and args.cuda else "cpu")
    ic(device)

    pred_price_n = 8 # Could be moved to argument
    
    max_action = config.action_space_high

    # Agents
    if args.agent == "SAC-sagg":
        agent = agentSAC_sagg(df_price, args, device, pred_price_n=pred_price_n).to(device)
        qf1 = SoftQNetwork(args).to(device)
        qf2 = SoftQNetwork(args).to(device)
    else:
        try:
            print(f"Attempting to load: {args.agent}")
            agent = torch.load(f"{config.agents_path}{args.agent}.pt")
            qf1 = torch.load(f"{config.agents_path}qf1_{args.agent}.pt")
            qf2 = torch.load(f"{config.agents_path}qf2_{args.agent}.pt")
            print(f"Loaded {args.agent}")
        except Exception as e:
            print(e)
            print(f"Agent name not recognized")
            exit(1)

    reward_coef = args.reward_coef
    proj_coef = args.proj_coef
    #ic(reward_coef, type(reward_coef))
    #ic(proj_coef, type(proj_coef))

    # Q networks
    qf1_target = SoftQNetwork(args).to(device)
    qf2_target = SoftQNetwork(args).to(device)
    qf1_target.load_state_dict(qf1.state_dict())
    qf2_target.load_state_dict(qf2.state_dict())
    q_optimizer = optim.Adam(list(qf1.parameters()) + list(qf2.parameters()), lr=args.q_lr)
    actor_optimizer = optim.Adam(list(agent.parameters()), lr=args.policy_lr)

    # Automatic entropy tuning
    if args.autotune:
        target_entropy = -torch.prod(torch.Tensor(args.n_action).to(device)).item()
        log_alpha = torch.zeros(1, requires_grad=True, device=device)
        alpha = log_alpha.exp().item()
        a_optimizer = optim.Adam([log_alpha], lr=args.q_lr)
    else:
        alpha = args.alpha

    #space_state  = gym.spaces.Box(low=np.array([-100_000]), high=np.array([100_000]), shape=np.array([args.n_state]), dtype=np.float32)
    space_state  = gym.spaces.Box(low=-100_000, high=100_000, shape=np.array([args.n_state]))
    space_action = gym.spaces.Box(low=0, high=1, shape=np.array([args.n_action]))
    rb = ReplayBuffer(
        args.buffer_size,
        #envs.single_observation_space,
        space_state,
        #envs.single_action_space,
        space_action,
        device,
        handle_timeout_termination=False,
    )
    start_time = time.time()


    # Add t_min, t_max
    if args.print_dash:
        print_welcome(df_sessions, df_price, contract_info)
        skips = 0


    ts_max = int(ts_min + 24 * 31)
    pbar = tqdm(desc=args.save_name, total=int(ts_max-ts_min)*args.years, smoothing=0.1)

    world = ChargeWorldEnv(df_sessions, df_price, contract_info, rng, skip_contracts = skip_contracts, norm_reward = args.norm_reward, lax_coef = args.lax_coef, df_imit = args.df_imit)

    for year in range(args.years):
        df_state = world.reset()
        obs = agent.df_to_state(df_state, ts_min) # should be ts_min -1 , but only matters for this timestep

        # Environment loop
        t = int(ts_min - 1)

        for global_step in range(args.total_timesteps):
            t += 1
            if t > ts_max: break
            pbar.update(1)

            # ALGO LOGIC: put action logic here
            ## !!! JS: CAREFUL!!!
            if global_step < args.learning_starts:
                actions = rng.uniform(low=config.action_space_low, high=config.action_space_high, size= args.n_action)
            else:
                actions, _, _ = agent.get_action(torch.Tensor(obs).to(device))
                actions = actions.detach().cpu().numpy()

            # Get agent.tostate(actions)
            df_state, rewards, terminations, infos = world.step(agent.action_to_env(actions))
            next_obs = agent.df_to_state(df_state, t)

            assert t == infos['t'], "Main time and env time out of sync"

            # Chec that actor --> agent
            if args.print_dash:
                if skips > 0: # Logic to jump forward
                    skips -= 1
                else:
                    usr_in = world.print(-1, clear = True)
                if usr_in.isnumeric():
                    skips = int(usr_in)
                    usr_in = ""
            else:
                pass
            ## !!! JS: CAREFUL!!!

            real_next_obs = next_obs.copy()

            # JS: No truncations
            #for idx, trunc in enumerate(truncations):
            #    if trunc:
            #        real_next_obs[idx] = infos["final_observation"][idx]
            rb.add(obs, real_next_obs, actions, rewards, terminations, infos)

            # TRY NOT TO MODIFY: CRUCIAL step easy to overlook
            obs = next_obs

            # ALGO LOGIC: training.
            if global_step > args.learning_starts:
                data = rb.sample(args.batch_size)
                with torch.no_grad():
                    next_state_actions, next_state_log_pi, _ = agent.get_action(data.next_observations)
                    qf1_next_target = qf1_target(data.next_observations, next_state_actions)
                    qf2_next_target = qf2_target(data.next_observations, next_state_actions)
                    min_qf_next_target = torch.min(qf1_next_target, qf2_next_target) - alpha * next_state_log_pi
                    next_q_value = data.rewards.flatten() + (1 - data.dones.flatten()) * args.gamma * (min_qf_next_target).view(-1)

                qf1_a_values = qf1(data.observations, data.actions).view(-1)
                qf2_a_values = qf2(data.observations, data.actions).view(-1)
                qf1_loss = F.mse_loss(qf1_a_values, next_q_value)
                qf2_loss = F.mse_loss(qf2_a_values, next_q_value)
                qf_loss = qf1_loss + qf2_loss

                # optimize the model
                q_optimizer.zero_grad()
                qf_loss.backward()
                q_optimizer.step()

                if global_step % args.policy_frequency == 0:  # TD 3 Delayed update support
                    for _ in range(
                        args.policy_frequency
                    ):  # compensate for the delay by doing 'actor_update_interval' instead of 1
                        pi, log_pi, _ = agent.get_action(data.observations)
                        qf1_pi = qf1(data.observations, pi)
                        qf2_pi = qf2(data.observations, pi)
                        min_qf_pi = torch.min(qf1_pi, qf2_pi)
                        actor_loss = ((alpha * log_pi) - min_qf_pi).mean()

                        actor_optimizer.zero_grad()
                        actor_loss.backward()
                        actor_optimizer.step()

                        if args.autotune:
                            with torch.no_grad():
                                _, log_pi, _ = agent.get_action(data.observations)
                            alpha_loss = (-log_alpha.exp() * (log_pi + target_entropy)).mean()

                            a_optimizer.zero_grad()
                            alpha_loss.backward()
                            a_optimizer.step()
                            alpha = log_alpha.exp().item()

                # update the target networks
                if global_step % args.target_network_frequency == 0:
                    for param, target_param in zip(qf1.parameters(), qf1_target.parameters()):
                        target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)
                    for param, target_param in zip(qf2.parameters(), qf2_target.parameters()):
                        target_param.data.copy_(args.tau * param.data + (1 - args.tau) * target_param.data)

        if not args.no_save:
            args.desc = f"_{year}"
            world.tracker.save_log(args, path=config.results_path)
            world.tracker.save_desc(args, {"title": title}, path=config.results_path)

        if args.save_contracts:
            world.tracker.save_contracts(args, path=config.results_path)

    # Save agent
    if args.save_agent:
        if args.save_name != "":
            torch.save(agent, f"{config.agents_path}{args.save_name}.pt")
            torch.save(qf1, f"{config.agents_path}qf1_{args.save_name}.pt")
            torch.save(qf2, f"{config.agents_path}qf2_{args.save_name}.pt")
        else:
            torch.save(agent, f"{config.agents_path}{world.tracker.timestamp}_{args.agent.split('.')[0]}{args.desc}.pt")
            torch.save(qf1, f"{config.agents_path}{world.tracker.timestamp}qf1_{args.agent.split('.')[0]}{args.desc}.pt")
            torch.save(qf2, f"{config.agents_path}{world.tracker.timestamp}qf2_{args.agent.split('.')[0]}{args.desc}.pt")

    pbar.close()

if __name__ == "__main__":
    args = parse_sac_args()

    if args.years is None:
        args.years = 1
    runSim(args)
    #else:
    #    og_save_name = args.save_name
    #    for i in range(args.years):
    #        if og_save_name != "":
    #            args.save_name = og_save_name + f"_{i}"
    #            if i > 0:
    #                args.agent = og_save_name + f"_{i-1}"
    #            runSim(args)
    #        else:
    #            raise Exception("You must specify save name to run multiple years")
