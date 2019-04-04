import torch
import torch.nn as nn
from torch.distributions import Categorical


class Flatten(nn.Module):
    def forward(self, x, action_reward_vector):
        out = x.view(x.size(0), -1)
        return torch.cat((out, action_reward_vector), dim=1)


class RecurrentPolicy(nn.Module):
    def __init__(self,
                 state_frame_channels: int,
                 action_space_size: int,
                 hidden_layer_size: int,
                 device: torch.device):
        super().__init__()

        self._cnn = nn.Sequential(
            nn.Conv2d(state_frame_channels, 32, 8, stride=4),
            nn.ReLU(),
            nn.Conv2d(32, 64, 4, stride=2),
            nn.ReLU(),
            nn.Conv2d(64, 32, 3, stride=1),
            nn.ReLU())
        self._flatten = Flatten()
        self._linear = nn.Sequential(
            nn.Linear(32 * 7 * 8 + action_space_size + 1, hidden_layer_size),
            nn.ReLU()
        )
        self._gru = nn.GRU(input_size=hidden_layer_size, hidden_size=hidden_layer_size)
        self._critic_linear = nn.Linear(hidden_layer_size, 1)
        self._actor_linear = nn.Linear(hidden_layer_size, action_space_size)
        self._action_space_size = action_space_size
        self.train()

        self._device = device
        self.to(device)

    def act(self, input_states, rnn_hxs, masks, prev_actions, prev_rewards):
        value, actor_features, rnn_hxs = self._base_forward(input_states,
                                                            rnn_hxs,
                                                            masks,
                                                            prev_actions,
                                                            prev_rewards)
        action, action_log_prob, action_entropy = self._sample_action(actor_features)
        return value, action, action_log_prob, action_entropy, rnn_hxs

    def value(self, input_states, rnn_hxs, masks, prev_actions, prev_rewards):
        value, _, _ = self._base_forward(input_states,
                                         rnn_hxs,
                                         masks,
                                         prev_actions,
                                         prev_rewards)
        return value.detach()

    def evaluate_actions(self,
                         input_states,
                         rnn_hxs,
                         masks,
                         prev_actions,
                         prev_rewards,
                         actions):
        value, actor_features, rnn_hxs = self._base_forward(input_states,
                                                            rnn_hxs,
                                                            masks,
                                                            prev_actions,
                                                            prev_rewards)
        distribution = self._action_distribution(actor_features)
        action_log_probs = distribution.log_prob(actions)
        action_entropy = distribution.entropy().mean()
        return value, action_log_probs, action_entropy

    def _base_forward(self, input_states, rnn_hxs, masks, prev_actions, prev_rewards):
        action_reward_vector = self._create_action_reward_vector(prev_actions,
                                                                 prev_rewards)
        value, actor_features, rnn_hxs = self(input_states,
                                              rnn_hxs,
                                              masks,
                                              action_reward_vector)
        return value, actor_features, rnn_hxs

    def forward(self, input_states, rnn_hxs, masks, action_reward_vector):
        cnn_out = self._cnn(input_states.type(torch.float32) / 255.0)
        flat_out = self._flatten(cnn_out, action_reward_vector)
        linear_out = self._linear(flat_out)
        x, rnn_hxs = self._forward_gru(linear_out, rnn_hxs, masks)
        return self._critic_linear(x), x, rnn_hxs

    def _forward_gru(self, x, rnn_hxs, masks):
        if x.size(0) == rnn_hxs.size(0):
            x, hxs = self._gru(x.unsqueeze(0), (rnn_hxs * masks).unsqueeze(0))
            x = x.squeeze(0)
            hxs = hxs.squeeze(0)
        else:
            num_envs_per_batch = rnn_hxs.size(0)
            steps_per_update = int(x.size(0) / num_envs_per_batch)

            x = x.view(steps_per_update, num_envs_per_batch, x.size(1))
            masks = masks.view(steps_per_update, num_envs_per_batch)

            # Figure out which steps in the sequence have a zero for any agent
            # Always assume t=0 has a zero in it as that makes the logic
            # cleaner.
            has_zeros = ((masks[1:] == 0.0)
                         .any(dim=-1)
                         .nonzero()
                         .squeeze()
                         .cpu())

            # +1 to correct the masks[1:]
            if has_zeros.dim() == 0:
                has_zeros = [has_zeros.item() + 1]
            else:
                has_zeros = (has_zeros + 1).numpy().tolist()

            # Add t=0 and t=T to the list
            has_zeros = [0] + has_zeros + [steps_per_update]

            hxs = rnn_hxs.unsqueeze(0)
            outputs = []
            for i in range(len(has_zeros) - 1):
                # Process steps that don't have any zeros in masks together.
                start_idx = has_zeros[i]
                end_idx = has_zeros[i + 1]

                rnn_scores, hxs = self._gru(x[start_idx:end_idx],
                                            hxs * masks[start_idx].view(1, -1, 1))
                outputs.append(rnn_scores)

            x = torch.cat(outputs, dim=0).view(steps_per_update * num_envs_per_batch, -1)
            hxs = hxs.squeeze(0)

        return x, hxs

    def _sample_action(self, actor_features):
        distribution = self._action_distribution(actor_features)
        action = distribution.sample()
        action_log_prob = distribution.log_prob(action)
        action_entropy = distribution.entropy().mean()
        return (action.unsqueeze(-1).long(),
                action_log_prob.unsqueeze(-1),
                action_entropy)

    def _action_distribution(self, actor_features):
        actor_out = self._actor_linear(actor_features)
        action_logits = nn.functional.log_softmax(actor_out, dim=1)
        distribution = Categorical(logits=action_logits)
        return distribution

    def _create_action_reward_vector(self, prev_actions, prev_rewards):
        action_reward_vector = torch.zeros(
            prev_actions.size(0),
            self._action_space_size + 1
        ).to(self._device)
        action_reward_vector.scatter_(1, prev_actions, 1)  # one-hot encoded actions
        action_reward_vector[:, -1] = prev_rewards.squeeze(-1)
        return action_reward_vector
