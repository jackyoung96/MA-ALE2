from typing import Dict, Any
import dill
import json
import argparse
import os
import torch
from algorithms.rainbow_nfsp import save_name
import numpy as np
import random
from experiment_train import trainer_types
import optuna
from optuna.trial import TrialState
import time
import ray
from all.experiments import MultiagentEnvExperiment
from param_samplers import sample_rainbow_params, sample_nfsp_rainbow_params, sample_ppo_params, sample_nfsp_ppo_params


parser = argparse.ArgumentParser(description="Run an multiagent Atari benchmark.")

parser.add_argument("--num-gpus", type=int, default=1,
                    help="number of GPUs for this search process")
parser.add_argument("--frames", type=int, default=50e6, help="The number of training frames.")
parser.add_argument("--frames-per-save", type=int, default=None)
parser.add_argument("--trainer-type", type=str, default="nfsp_rainbow")
parser.add_argument("--num-eval-episodes", type=int, default=20,
                    help="how many evaluation episodes to run per training epoch")
parser.add_argument("--local", action='store_true', default=False,
                    help="create study locally (no SQL database)")
parser.add_argument("--envs", type=str, required=True,
                    help="must be comma-separated list of envs with no spaces!")
parser.add_argument("--study-name", type=str, default=None,
                    help="name of shared Optuna study for distributed training")
parser.add_argument("--study-create", default=False, action="store_true",
                    help="will create study if does not already exist")
parser.add_argument("--db-name", type=str, default="maale",
                    help="name of SQL table name. Uses old name as default for testing purposes.")
parser.add_argument("--db-password", type=str)
parser.add_argument("--db-user", type=str, default='database')
parser.add_argument("--max-trials", type=int, default=100,
                    help="number of trials for EACH environment, or how many times hparams are sampled.")
parser.add_argument("--from-ckpt", action="store_true",
                    help="learning start from previous trained checkpoints")
args = parser.parse_args()
args.device = 'cuda' if args.num_gpus > 0 else 'cpu'

if args.device == 'cuda':
    print('CUDA_VISIBLE_DEVICES:', os.environ['CUDA_VISIBLE_DEVICES'])
    os.environ['CUDA_VISIBLE_DEVICES'] = ",".join([str(i) for i in range(args.num_gpus)])


SQL_ADDRESS = f"mysql://{args.db_user}:{args.db_password}@35.194.57.226/{args.db_name}"

env_list = args.envs.split(',')



with open("plot_data/builtin_env_rewards.json", "r") as fd:
    builtin_rewards = json.load(fd)
with open("plot_data/rand_rewards.json", "r") as fd:
    rand_rewards = json.load(fd)


if args.trainer_type in ["shared_rainbow", "parallel_rainbow"]:
    sampler_fn = sample_rainbow_params
elif args.trainer_type in ["nfsp_rainbow", "parallel_rainbow_nfsp"]:
    sampler_fn = sample_nfsp_rainbow_params
elif args.trainer_type == "shared_ppo":
    sampler_fn = sample_ppo_params
elif args.trainer_type == "nfsp_ppo":
    sampler_fn = sample_nfsp_ppo_params
else:
    raise ValueError

def normalize_score(score: np.ndarray, env_id: str) -> np.ndarray:
    """
    Normalize score to be in [0, 1] where 1 is maximal performance.
    :param score: unnormalized score
    :param env_id: environment id
    :return: normalized score
    """
    assert env_id in rand_rewards
    assert env_id in builtin_rewards

    rand_score = max(rand_rewards[env_id]['mean_rewards'].values())
    builtin_score = builtin_rewards[env_id]['mean_rewards']['first']
    return (score - builtin_score) / (rand_score - builtin_score)


@ray.remote(num_gpus=args.num_gpus/len(env_list), max_calls=len(env_list))
def train(hparams, seed, trial, env_id):
    # set all hparams sampled from the trial
    buffer_size = hparams.get('replay_buffer_size', None)

    # use non-parallel rainbow nfsp if reservoir buffer is too large for RAM
    experiment, preset, env = trainer_types[args.trainer_type](
        env_id, args.device, buffer_size,
        seed=seed,
        num_frames=args.frames,
        hparams=hparams,
        quiet=False,
    )

    is_ma_experiment = isinstance(experiment, MultiagentEnvExperiment)
    if is_ma_experiment: # TODO: not supported by ParallelEnvExperiment yet
        experiment.seed_env(seed)

    save_folder = "checkpoint/" + save_name(args.trainer_type, env_id, buffer_size, args.frames, seed)
    norm_eval_returns = []
    norm_return, avg_norm_return = None, None

    if not os.path.isdir(save_folder):
        os.makedirs(save_folder)
    num_frames_train = int(args.frames)
    frames_per_save = args.frames_per_save or max(num_frames_train // 100, 1)

    # Start from the last preset
    frame_start = 0
    if args.from_ckpt:
        if len(os.listdir(save_folder)) != 0:
            frame_start = sorted([int(ckpt.strip('.pt')) for ckpt in os.listdir(save_folder)])[-1]
            preset = torch.load(f"{save_folder}/{frame_start:09d}.pt")

    if not is_ma_experiment:
        num_envs = int(experiment._env.num_envs)
        returns = np.zeros(num_envs)
        state_array = env.reset()
        start_time = time.time()
        completed_frames = 0
        experiment._frame = frame_start

        while experiment._frame <= num_frames_train:
            action = experiment._agent.act(state_array)
            state_array = env.step(action)
            experiment._frame += num_envs
            episodes_completed = state_array.done.type(torch.IntTensor).sum().item()
            completed_frames += num_envs
            returns += state_array.reward.cpu().detach().numpy()
            if episodes_completed > 0:
                dones = state_array.done.cpu().detach().numpy()
                cur_time = time.time()
                fps = completed_frames / (cur_time - start_time)
                completed_frames = 0
                start_time = cur_time
                for i in range(num_envs):
                    if dones[i]:
                        experiment._log_training_episode(returns[i], fps)
                        returns[i] = 0
            experiment._episode += episodes_completed

            if (experiment._frame % frames_per_save) < num_envs:
                # time to save and eval
                torch.save(preset, f"{save_folder}/{experiment._frame:09d}.pt")

                # ParallelExperiment returns both agents' rewards in a single list: slice to get first agent's
                n_agents = 2
                eval_returns = experiment.test(episodes=args.num_eval_episodes * n_agents)
                eval_returns = eval_returns[::n_agents]

                mean_return = np.mean(eval_returns)
                norm_return = normalize_score(mean_return, env_id=env_id)
                norm_eval_returns.append(norm_return)
                avg_norm_return = np.mean(norm_eval_returns)

                # Handle pruning based on the intermediate value.
                trial.report(value=avg_norm_return, step=experiment._frame)
                if trial.should_prune():
                    raise optuna.exceptions.TrialPruned()
    else:
        for frame in range(frame_start, num_frames_train, frames_per_save):
            experiment.train(frames=frame)
            torch.save(preset, f"{save_folder}/{frame + frames_per_save:09d}.pt")

            eval_returns = experiment.test(episodes=args.num_eval_episodes)
            assert len(eval_returns) == 1
            eval_returns = list(eval_returns.values())[0]
            experiment._save_model()  # not implemented in Parallel yet

            mean_return = np.mean(eval_returns)
            norm_return = normalize_score(mean_return, env_id=env_id)
            norm_eval_returns.append(norm_return)
            avg_norm_return = np.mean(norm_eval_returns)

            # Handle pruning based on the intermediate value.
            trial.report(value=avg_norm_return, step=frame + frames_per_save)
            if trial.should_prune():
                raise optuna.exceptions.TrialPruned()

    # if ckpt is already fully existed
    if frame_start >= num_frames_train:
        eval_returns = experiment.test(episodes=args.num_eval_episodes)
        if is_ma_experiment: # MultiAgentEnvExperiment returns dict, but evals are always one key
            assert len(eval_returns) == 1
            eval_returns = list(eval_returns.values())[0]
            experiment._save_model()  # not implemented in Parallel yet
        # for aid, returns in eval_returns.items():
        mean_return = np.mean(eval_returns)
        norm_return = normalize_score(mean_return, env_id=env_id)
        norm_eval_returns.append(norm_return)
        avg_norm_return = np.mean(norm_eval_returns)

        # Handle pruning based on the intermediate value.
        trial.report(value=avg_norm_return, step=experiment._frame + frames_per_save)

    # clean up?
    del experiment, preset, env

    return avg_norm_return


N_TRIALS = -1
def objective_all(trial):
    """Get hyperparams for trial"""
    global N_TRIALS
    N_TRIALS = trial.number

    hparams = sampler_fn(trial)

    seed = trial.number
    np.random.seed(seed)
    random.seed(seed)
    torch.manual_seed(seed)

    futures = [train.remote(hparams, seed, trial, env_id) for env_id in env_list]
    norm_returns = ray.get(futures)

    print(hparams)
    print(norm_returns)

    return np.mean(norm_returns)


if __name__ == "__main__":
    if args.local:
        ray.init(num_gpus=args.num_gpus, local_mode=True)
        time.sleep(10)
        study = optuna.create_study(direction="maximize",
                                    study_name=args.study_name,
                                    load_if_exists=True)
    else:
        import pathlib
        temp_dir = pathlib.Path(__file__).parent.resolve().joinpath("raytmp")
        ray.init(num_gpus=args.num_gpus, local_mode=False, _temp_dir=str(temp_dir))
        time.sleep(10)
        if args.study_create:
            study = optuna.create_study(direction="maximize",
                                        storage=SQL_ADDRESS,
                                        study_name=args.study_name,
                                        load_if_exists=True)
        else:
            study = optuna.load_study(study_name=args.study_name,
                                      storage=SQL_ADDRESS)

    while N_TRIALS < args.max_trials:
        study.optimize(objective_all, n_trials=1, timeout=600)

    pruned_trials = study.get_trials(deepcopy=False, states=[TrialState.PRUNED])
    complete_trials = study.get_trials(deepcopy=False, states=[TrialState.COMPLETE])

    print("Study statistics: ")
    print("  Number of finished trials: ", len(study.trials))
    print("  Number of pruned trials: ", len(pruned_trials))
    print("  Number of complete trials: ", len(complete_trials))

    print("Best trial:")
    trial = study.best_trial

    print("  Value: ", trial.value)

    print("  Params: ")
    for key, value in trial.params.items():
        print("    {}: {}".format(key, value))

    with open(f"best_params_{args.env}.pkl", 'wb') as fd:
        dill.dump(trial.params, fd)
