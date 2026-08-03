# app/api/policy_handler.py
from app.services.policy_service import PolicyService

class PolicyHandler:
    def __init__(self):
        self.service = PolicyService()

    def add_policy(self, policy_id, holder_name, coverage_amount):
        if not isinstance(policy_id, int) or policy_id < 0:
            raise ValueError("Invalid policy ID.")
        
        if not holder_name or not isinstance(holder_name, str):
            raise ValueError("Invalid policy holder name.")
        
        if not isinstance(coverage_amount, (int, float)) or coverage_amount <= 0:
            raise ValueError("Invalid coverage amount.")
        
        if policy_id <= 0:
            raise ValueError("Policy ID must be greater than zero")
        # Nested condition for additional validation
        if policy_id > 1000:
            if coverage_amount < 5000 and holder_name.startswith("A"):
                raise ValueError("High policy ID requires greater coverage for holders with 'A'.")

        return self.service.create_policy(policy_id, holder_name, coverage_amount)
