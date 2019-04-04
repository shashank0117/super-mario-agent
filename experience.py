from typing import Tuple

import torch


class ExperienceBatch:

    def __init__(self,
                 observations,
                 actions,
                 prev_actions,
                 prev_rewards,
                 action_log_probs,
                 returns,
                 value_predictions,
                 advantage_targets,
                 masks,
                 recurrent_hidden_states):
        num_steps, num_envs = actions.shape[:2]
        self.observations = self._flatten(observations, num_steps, num_envs)
        self.actions = self._flatten(actions, num_steps, num_envs)
        self.prev_actions = self._flatten(prev_actions, num_steps, num_envs)
        self.prev_rewards = self._flatten(prev_rewards, num_steps, num_envs)
        self.action_log_probs = self._flatten(action_log_probs, num_steps, num_envs)
        self.returns = self._flatten(returns, num_steps, num_envs)
        self.value_predictions = self._flatten(value_predictions, num_steps, num_envs)
        self.advantage_targets = self._flatten(advantage_targets, num_steps, num_envs)
        self.masks = self._flatten(masks, num_steps, num_envs)
        self.recurrent_hidden_states = recurrent_hidden_states.view(num_envs, -1)

    def action_eval_input(self):
        return (self.observations,
                self.recurrent_hidden_states,
                self.masks,
                self.prev_actions,
                self.prev_rewards,
                self.actions)

    def _flatten(self, tensor, num_steps, num_envs):
        return tensor.view(num_steps * num_envs, *tensor.shape[2:])


class ExperienceStorage:

    def __init__(self,
                 num_steps: int,
                 num_envs: int,
                 observation_shape: Tuple,
                 recurrent_hidden_state_size: int):
        self._num_steps = num_steps
        self._num_envs = num_envs
        self._step = 0

        self.observations = torch.zeros(num_steps + 1, num_envs, *observation_shape)
        self.actions = torch.zeros(num_steps, num_envs, 1, dtype=torch.long)
        self.action_log_probs = torch.zeros(num_steps, num_envs, 1)
        self.rewards = torch.zeros(num_steps, num_envs, 1)
        self.value_predictions = torch.zeros(num_steps + 1, num_envs, 1)
        self.returns = torch.zeros(num_steps + 1, num_envs, 1)
        self.masks = torch.ones(num_steps + 1, num_envs, 1)
        self.recurrent_hidden_states = torch.zeros(num_steps + 1,
                                                   num_envs,
                                                   recurrent_hidden_state_size)

    def insert(self,
               observations,
               actions,
               action_log_probs,
               rewards,
               value_predictions,
               masks,
               recurrent_hidden_states):
        self.observations[self._step + 1].copy_(observations)
        self.actions[self._step].copy_(actions)
        self.action_log_probs[self._step].copy_(action_log_probs)
        self.rewards[self._step].copy_(rewards)
        self.value_predictions[self._step].copy_(value_predictions)
        self.masks[self._step + 1].copy_(masks)
        self.recurrent_hidden_states[self._step + 1].copy_(recurrent_hidden_states)
        self._step = (self._step + 1) % self._num_steps

    def insert_initial_observations(self, observations):
        self.observations[0].copy_(observations)

    def get_actor_input(self, step):
        states = self.observations[step]
        rnn_hxs = self.recurrent_hidden_states[step]
        masks = self.masks[step]
        prev_actions = self.actions[step - 1]
        prev_rewards = self.rewards[step - 1]
        return states, rnn_hxs, masks, prev_actions, prev_rewards

    def get_critic_input(self):
        return self.get_actor_input(step=-1)

    def compute_returns(self, next_value, discount, gae_lambda):
        self.value_predictions[-1] = next_value
        gae = 0
        for step in reversed(range(self.rewards.size(0))):
            delta = self.rewards[step] + \
                discount * self.value_predictions[step + 1] * self.masks[step + 1] - \
                self.value_predictions[step]
            gae = delta + discount * gae_lambda * self.masks[step + 1] * gae
            self.returns[step] = gae + self.value_predictions[step]

    def after_update(self):
        self.observations[0].copy_(self.observations[-1])
        self.recurrent_hidden_states[0].copy_(self.recurrent_hidden_states[-1])
        self.masks[0].copy_(self.masks[-1])

    def _compute_advantages(self, eps: float=1e-5):
        advantages = self.returns[:-1] - self.value_predictions[:-1]
        norm_advantages = (advantages - advantages.mean()) / (advantages.std() + eps)
        return norm_advantages

    def batches(self, minibatches: int):
        """Yield experience batches for recurrent policy training."""
        assert (self._num_envs % minibatches) == 0
        num_envs_per_batch = self._num_envs // minibatches
        random_env_indices = torch.randperm(self._num_envs)

        advantages = self._compute_advantages()

        for start in range(0, self._num_envs, num_envs_per_batch):
            end = start + num_envs_per_batch
            indices = random_env_indices[start:end]

            prev_actions = torch.zeros(self._num_steps, num_envs_per_batch, 1)
            prev_actions[1:, :] = self.actions[:-1, indices]

            prev_rewards = torch.zeros(self._num_steps, num_envs_per_batch, 1)
            prev_rewards[1:, :] = self.rewards[:-1, indices]

            yield ExperienceBatch(
                observations=self.observations[:-1, indices],
                actions=self.actions[:, indices],
                prev_actions=prev_actions.type(torch.long),
                prev_rewards=prev_rewards,
                action_log_probs=self.action_log_probs[:, indices],
                returns=self.returns[:-1, indices],
                value_predictions=self.value_predictions[:-1, indices],
                advantage_targets=advantages[:, indices],
                masks=self.masks[:-1, indices],
                recurrent_hidden_states=self.recurrent_hidden_states[:1, indices]
            )
