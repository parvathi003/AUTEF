# app/data/policy_repository.py

class PolicyRepository:
    def __init__(self, database):
        self.db = database

    def add_policy(self, policy_id, data):
        if not isinstance(policy_id, int) or policy_id < 0:
            raise ValueError("Invalid policy ID.")
        if not isinstance(data, dict) or not data:
            raise ValueError("Policy data must be a non-empty dictionary.")
        return self.db.insert(policy_id, data)

    def get_policy(self, policy_id):
        if not isinstance(policy_id, int) or policy_id < 0:
            raise ValueError("Invalid policy ID.")
        return self.db.fetch(policy_id)

    def remove_policy(self, policy_id):
        if not isinstance(policy_id, int) or policy_id < 0:
            raise ValueError("Invalid policy ID.")
        return self.db.delete(policy_id)

