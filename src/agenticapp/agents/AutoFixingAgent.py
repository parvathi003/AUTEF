import json
import os
import re
from autogen import AssistantAgent,config_list_from_json
from template_prompts import build_fix_prompt
from agenticapp.agents.CoverageAgent import update_test_files
from dotenv import load_dotenv
# Load environment variables
load_dotenv()
# Load AI agent configurations
config_path = r"D:\Sai\EnhanceUnitTesting\src\agenticapp\OAI_CONFIG_LIST.json"
config_list = config_list_from_json(config_path)
# Replace the placeholder with the actual API key from .env
for config in config_list:
    if "api_key" in config and config["api_key"] == "ENV_OPENAI_API_KEY":
        config["api_key"] = os.getenv("OPENAI_API_KEY")



# Initialize OpenAI Agent
#llm_config = {
#    "model": "gpt-4o-mini",  # Ensure this is a valid field
#    "temperature": 0.7,  # Example of another valid key
#}

# Initialize AI Agents
openai_agent = AssistantAgent(
    name="AutoFixingAgent",
    llm_config={"config_list": config_list, "temperature": 0},
)

#openai_agent = AssistantAgent(name="AutoFixingAgent",llm_config=llm_config)

# Paths for test and source code files
TEST_FOLDER = "tests"
TEST_RESULTS_FILE = "results.json"



# Load test results
def load_test_results(TEST_RESULTS_FILE):
    with open(TEST_RESULTS_FILE, "r") as file:
        return json.load(file)["results"]

# Identify failed tests
def extract_failed_tests(test_results):
    return [{"name": test["test"], "reason": test.get("reason", "Unknown error")} for test in test_results if test["status"] in ["FAIL", "ERROR"]]
import re

def locate_test_file(test_name):
    """ Extracts the correct test file path and searches recursively. """
    test_parts = test_name.split(".")[:-1]  # Remove function name
    class_name = test_parts[-1]  # Extract class name (e.g., TestOrderRepository)

    # Convert CamelCase class name to snake_case file name
    file_base_name = re.sub(r'(?<!^)(?=[A-Z])', '_', class_name).lower()

    # Ensure correct prefix (avoid double "test_" issues)
    expected_file_prefix = file_base_name if file_base_name.startswith("test_") else f"test_{file_base_name}"
    
    test_subfolder = os.path.join(*test_parts[:-1])  # Extract expected folder structure

    print(f"[DEBUG] Searching for a test file matching: {expected_file_prefix} in {TEST_FOLDER}")

    for root, _, files in os.walk(TEST_FOLDER):
        for file in files:
            full_path = os.path.join(root, file)
            normalized_full_path = full_path.replace("\\", "/")  # Normalize path
            normalized_subfolder = test_subfolder.replace("\\", "/")

            # Match file name using prefix check (handles test_order_repository_0.py cases)
            if file.startswith(expected_file_prefix) and file.endswith(".py") and normalized_subfolder in normalized_full_path:
                print(f"[FOUND] Test file located: {full_path}")
                return full_path

    print(f"[ERROR] Test file for {test_name} not found.")
    return None

import re

def extract_test_function(test_path, test_name):
    """ Extracts the full test function from the test file. """
    with open(test_path, "r", encoding="utf-8") as file:
        content = file.read()

    test_function_name = test_name.split(".")[-1]  # Extract the function name

    # Improved regex pattern to match full function block
    test_function_pattern = rf"(?:@[\w]+\s+)?def {test_function_name}\(.*?\):\s*(?:\"\"\".*?\"\"\"\s*)?(.*?)(?=\n\ndef |\n*$)"

    match = re.search(test_function_pattern, content, re.DOTALL)

    if match:
        print(f"[SUCCESS] Extracted function: {test_function_name} from {test_path}")
        return match.group(), content
    else:
        print(f"[ERROR] Test function {test_name} not found in {test_path}")
        return None, content


def fix_test_case(source_code,test_function_code, error_reason):
    
    messages=build_fix_prompt(source_code, test_function_code, error_reason)
    response = openai_agent.generate_oai_reply( messages)
    #response = openai_agent.generate_reply(messages)
    print("response is ",response)
    if not response :
        print("[ERROR] No response from OpenAI for:", test_function_code)
        return None

    #fixed_code = response["content"].strip()
    fixed_code = response[1].strip() if isinstance(response, tuple) else response.strip()
    if fixed_code == test_function_code:
        print("[WARNING] OpenAI did not modify the test case:", test_function_code)
        return None

    print("[SUCCESS] Fixed test case generated.")
    return fixed_code


# Apply regex-based fixes for common errors
def apply_regex_fix(test_name, test_function_code, error_reason):
    if "AssertionError" in error_reason:
        # Fix string formatting issues (e.g., "{1}" -> "1")
        if "!=" in error_reason:
            test_function_code = re.sub(r"{(\d+)}", r"\1", test_function_code)
            print(f"Auto-fixed string format issue in {test_name}")
        return test_function_code

    if "AttributeError" in error_reason:
        # Fix attribute mismatches (e.g., name → item)
        test_function_code = re.sub(r"\.name", ".item", test_function_code)
        print(f"Auto-fixed attribute mismatch in {test_name}")
        return test_function_code

    return None


def locate_source_file(test_name, SOURCE_FOLDER_PATH):
    """Locate the corresponding source file based on test name structure and class."""
    test_parts = test_name.split(".")[:-1]  # Remove function name
    test_class = test_parts[-1]  # e.g., TestPolicyService

    # Convert CamelCase to snake_case
    file_base_name = re.sub(r'(?<!^)(?=[A-Z])', '_', test_class).lower()
    print("file base name:", file_base_name)

    # Remove 'test_' prefix if present
    if file_base_name.startswith("test_"):
        file_base_name = file_base_name[5:]

    source_file_name = f"{file_base_name}.py"
    source_subfolder = os.path.join(*test_parts[0:-2])  # Adjusted here

    print(f"[DEBUG] Searching for a source file matching: {source_file_name} in {SOURCE_FOLDER_PATH}/{source_subfolder}")

    for root, _, files in os.walk(SOURCE_FOLDER_PATH):
        for file in files:
            full_path = os.path.join(root, file)
            normalized_path = full_path.replace("\\", "/")
            normalized_target = os.path.join(SOURCE_FOLDER_PATH, source_subfolder).replace("\\", "/")

            if file == source_file_name and normalized_target in normalized_path:
                print(f"[FOUND] Source file located: {full_path}")
                return full_path

    print(f"[ERROR] Source file for {test_name} not found.")
    return None
def update_test_file(test_path, test_name, error_reason):
    # Extract function if it exists
    test_function_code, content = extract_test_function(test_path, test_name)

    # Try regex fix
    fixed_code = apply_regex_fix(test_name, test_function_code, error_reason) if test_function_code else None

    # If regex fix fails, fall back to LLM
    if not fixed_code:
        print(f"[INFO] Using OpenAI to fix or create {test_name}")
        SOURCE_FOLDER_PATH = r"D:\Sai\EnhanceUnitTesting\source_files"
        source_path = locate_source_file(test_name, SOURCE_FOLDER_PATH)

        if not source_path:
            print(f"[ERROR] Source file not found for {test_name}, skipping OpenAI fix.")
            return

        with open(source_path, "r", encoding="utf-8") as source_file:
            source_code = source_file.read()

        fixed_code = fix_test_case(source_code, test_function_code, error_reason)

    if not fixed_code:
        print(f"[ERROR] Failed to generate a fix for {test_name}")
        return

    if test_function_code:
        # Remove the existing failing function
        content = content.replace(test_function_code, "").strip()

        with open(test_path, "w", encoding="utf-8") as file:
            file.write(content)

        print(f"[INFO] Deleted failing function: {test_name} from {test_path}")

    # Append the fixed or new test function using update_test_files
    update_test_files(test_path, fixed_code)
    print(f"[SUCCESS] Appended modified test case: {test_name} into {test_path}")

# Main function
def fix_failing_tests(TEST_RESULTS_FILE):
    test_results = load_test_results(TEST_RESULTS_FILE)
    failed_tests = extract_failed_tests(test_results)

    if not failed_tests:
        print("No failing tests found.")
        return

    for test in failed_tests:
        test_path = locate_test_file(test["name"])
        if test_path:
            update_test_file(test_path, test["name"], test["reason"])

if __name__ == "__main__":
    IMPROVED_TEST_RESULTS_FILE = "improved_testcaseresults.json"
    fix_failing_tests(IMPROVED_TEST_RESULTS_FILE)
