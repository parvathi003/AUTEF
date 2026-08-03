# app/data/database.py
class Database:
    def __init__(self):
        self.policies = {}

    def insert(self, policy_id, data):
        if not isinstance(policy_id, int) or policy_id < 0:
            raise ValueError("Policy ID must be a positive integer.")
        if not isinstance(data, dict):
            raise TypeError("Policy data must be a dictionary.")
        if policy_id in self.policies:
            return False  # Policy already exists
        self.policies[policy_id] = data
        return True

    def fetch(self, policy_id):
        if not isinstance(policy_id, int) or policy_id < 0:
            raise ValueError("Invalid policy ID.")
        return self.policies.get(policy_id, None)

    def delete(self, policy_id):
        if not isinstance(policy_id, int) or policy_id < 0:
            raise ValueError("Invalid policy ID.")

        deleted = self.policies.pop(policy_id, None)
        return deleted if deleted is not None else None  # Explicitly return None
