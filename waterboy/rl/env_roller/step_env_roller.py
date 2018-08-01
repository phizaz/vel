import torch
import numpy as np


class StepEnvRoller:
    """
    Class calculating env rollouts.
    Idea behind this class is to store as much as we can as pytorch tensors to minimize tensor copying.
    """

    def __init__(self, environment, device, number_of_steps, discount_factor):
        self.environment = environment
        self.device = device
        self.number_of_steps = number_of_steps
        self.discount_factor = discount_factor

        # Initial observation
        self.observation = self._to_tensor(self.environment.reset())
        self.dones = torch.tensor([False for _ in range(self.observation.shape[0])], device=self.device)

        self.batch_observation_shape = (
            (self.observation.shape[0]*self.number_of_steps,) + self.environment.observation_space.shape
        )

    def _to_tensor(self, numpy_array):
        """ Convert numpy array to a tensor """
        return torch.from_numpy(numpy_array).to(self.device)

    @torch.no_grad()
    def rollout(self, model):
        """ Calculate env rollout """
        observation_accumulator = []  # Device tensors
        action_accumulator = []  # Device tensors
        value_accumulator = []  # Device tensors
        dones_accumulator = []  # Device tensors
        rewards_accumulator = []  # Device tensors
        episode_information = []  # Python objects

        for step_idx in range(self.number_of_steps):
            actions, values, _ = model.step(self.observation)

            observation_accumulator.append(self.observation)
            action_accumulator.append(actions)
            value_accumulator.append(values)
            dones_accumulator.append(self.dones)

            actions_numpy = actions.detach().cpu().numpy()
            new_obs, new_rewards, new_dones, new_infos = self.environment.step(actions_numpy)

            # Done is flagged true when the episode has ended AND the frame we see is already a first frame from the
            # Next episode
            self.dones = self._to_tensor(new_dones.astype(np.uint8))
            self.observation = self._to_tensor(new_obs)

            rewards_accumulator.append(self._to_tensor(new_rewards.astype(np.float32)))

            for info in new_infos:
                maybe_episode_info = info.get('episode')

                if maybe_episode_info:
                    episode_information.append(maybe_episode_info)

        last_values = model.value(self.observation)
        dones_accumulator.append(self.dones)

        observation_buffer = torch.stack(observation_accumulator)
        rewards_buffer = torch.stack(rewards_accumulator)
        # There may be different types of actions
        actions_buffer = torch.stack(action_accumulator)
        values_buffer = torch.stack(value_accumulator)
        dones_buffer = torch.stack(dones_accumulator)

        masks_buffer = dones_buffer[:-1, :]
        dones_buffer = dones_buffer[1:, :]

        discounted_rewards = self.discount_bootstrap(rewards_buffer, dones_buffer, last_values)

        # Reshape into final batch size
        return {
            'observations': observation_buffer.reshape(self.batch_observation_shape),
            'discounted_rewards': discounted_rewards.flatten(),
            'masks': masks_buffer.flatten(),
            'actions': actions_buffer.flatten(),
            'values': values_buffer.flatten(),
            'advantages': discounted_rewards.flatten() - values_buffer.flatten(),
            'episode_information': episode_information
        }

    def discount_bootstrap(self, rewards_buffer, dones_buffer, last_values_buffer):
        true_value_buffer = torch.zeros_like(rewards_buffer)
        dones_buffer = dones_buffer.to(dtype=torch.float32)

        # discount/bootstrap off value fn
        current_value = last_values_buffer

        for i in reversed(range(self.number_of_steps)):
            current_value = rewards_buffer[i] + self.discount_factor * current_value * (1.0 - dones_buffer[i])
            true_value_buffer[i] = current_value

        return true_value_buffer
