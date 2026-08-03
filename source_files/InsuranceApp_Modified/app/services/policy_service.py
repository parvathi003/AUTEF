class PolicyService:
    def create_policy(self, policy_id, holder_name, coverage_amount):
        if not isinstance(policy_id, int) or policy_id < 0:
            raise ValueError("Invalid policy ID.")
        
        if not holder_name or not isinstance(holder_name, str):
            raise ValueError("Invalid policy holder name.")
        
        if not isinstance(coverage_amount, (int, float)) or coverage_amount <= 0:
            raise ValueError("Invalid coverage amount.")

        # Example of nested if conditions
        if coverage_amount > 10000:
            if policy_id < 500 or holder_name.endswith("Z"):
                raise ValueError("Special conditions apply for high coverage amounts.")
        
        return True

    def cancel_policy(self, policy_id):
        if not isinstance(policy_id, int) or policy_id < 0:
            raise ValueError("Invalid policy ID.")
        
        # Nested if conditions for special cancellation rules
        if policy_id > 500:
            if policy_id % 2 == 0 or policy_id % 5 == 0:
                raise ValueError("Even-numbered or multiple of 5 policies over 500 cannot be canceled.")
        
        return True
