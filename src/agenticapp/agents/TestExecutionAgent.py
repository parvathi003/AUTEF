import autogen
from autogen import AssistantAgent, config_list_from_json
import unittest
import json
import os
from dotenv import load_dotenv
from datetime import datetime
import pandas as pd
import importlib
import shutil
# Load environment variables
load_dotenv()
# Load AI agent configurations
config_path = r"D:\Sai\EnhanceUnitTesting\src\agenticapp\OAI_CONFIG_LIST.json"
config_list = config_list_from_json(config_path)
# Replace the placeholder with the actual API key from .env
for config in config_list:
    if "api_key" in config and config["api_key"] == "ENV_OPENAI_API_KEY":
        config["api_key"] = os.getenv("OPENAI_API_KEY")


# Initialize AI Agents
test_execution_agent = AssistantAgent(
    name="TestExecutionAgent",
    llm_config={"config_list": config_list, "temperature": 0},
)


def discover_and_run_tests(test_folder, output_json="test_results.json", output_html="test_results.html"):
    """
    Uses test_execution_agent to discover and run all unit tests in a folder.
    Stores test results in JSON and HTML reports.
    """
    
    CACHE_PATH = os.path.join(test_folder, "__pycache__")  # Define cache path dynamically
    print("TestExecutionAgent: Running unit tests...")
    # Ensure cache is removed before running tests
    if os.path.exists(CACHE_PATH):
        shutil.rmtree(CACHE_PATH, ignore_errors=True)
        print(f"✅ Deleted cache folder: {CACHE_PATH}")  # Debugging
    # Force reload of test modules to prevent stale imports
    for root, _, files in os.walk(test_folder):
        for file in files:
            if file.startswith("test_") and file.endswith(".py"):
                module_name = os.path.splitext(file)[0]
                module_path = os.path.relpath(os.path.join(root, module_name), start=test_folder).replace(os.sep, ".")

                try:
                    module = importlib.import_module(module_path)
                    importlib.reload(module)
                    print(f"🔄 Reloaded module: {module_path}")
                except ModuleNotFoundError:
                    print(f"⚠️ Module not found: {module_path}")    
    test_execution_agent.generate_reply(
    messages=[{"role": "system", "content": "Execute unit tests automatically without user input."}]
    )
   
    loader = unittest.defaultTestLoader
    topleveldir="D:\\Sai\\EnhanceUnitTesting\\tests"
   
    suite = loader.discover(test_folder, pattern="test_*.py",top_level_dir=test_folder)
    
    if suite.countTestCases() == 0:
        print("No test cases found.")
        return

    results = {"test_run_date": datetime.now().isoformat(), "results": []}
    test_runner = unittest.TextTestRunner(verbosity=2)
    result = test_runner.run(suite)
    
    #suite = unittest.TestLoader().discover(test_folder,pattern="test_*.py",top_level_dir=test_folder)
    suite = unittest.TestLoader().discover(test_folder,pattern="test_*.py",top_level_dir=topleveldir)
    if suite.countTestCases() == 0:
        print("No test cases found in the specified folder.")
        return
    
    all_test_cases = []
    _collect_test_cases(suite, all_test_cases)
    print(f"✅ Collected {len(all_test_cases)} test cases after recursion.")
    
    for test_case in all_test_cases: # loop over test cases and add results
        add_test_result(results["results"], test_case, result)
   
    with open(output_json, "w") as f:
        json.dump(results, f, indent=4)

    generate_html_report(results["results"], output_html)
    print(f"Test results stored in {output_json} and {output_html}")

def _collect_test_cases(test_or_suite, all_test_cases):
    """ Recursively collects test cases from a suite or individual test case. """
    if isinstance(test_or_suite, unittest.TestSuite):
        for test in test_or_suite:
            _collect_test_cases(test, all_test_cases)  # Recursively process nested suites
    elif isinstance(test_or_suite, unittest.TestCase):
        print(f"✔️ Collected test: {test_or_suite.id()}")  # Debugging statement
        all_test_cases.append(test_or_suite)  # Store test cases

def add_test_result(results_list, test, result):
    """ Helper function to format test results. """
    test_id = test.id()
    status = "PASS"
    reason = None

    for failed_test, failure_reason in result.failures:
        if failed_test.id() == test_id:
            status = "FAIL"
            reason = str(failure_reason)
            break

    for error_test, error_reason in result.errors:
        if error_test.id() == test_id:
            status = "ERROR"
            reason = str(error_reason)
            break

    results_list.append({"test": test_id, "status": status, "reason": reason})


def generate_html_report(test_results, output_file):
    """
    Generate a beautified HTML report with test results in a tabular format.
    
    :param test_results: List of test results.
    :param output_file: The file to store the HTML report.
    """
    # Create a DataFrame for easy HTML generation
    df = pd.DataFrame(test_results)

    # Add custom styles for pass/fail/error rows
    def apply_status_color(row):
        if row['status'] == 'PASS':
            return ['background-color: #d4edda;' for _ in row]  # Green background for PASS
        elif row['status'] == 'FAIL':
            return ['background-color: #f8d7da;' for _ in row]  # Red background for FAIL
        elif row['status'] == 'ERROR':
            return ['background-color: #fff3cd;' for _ in row]  # Yellow background for ERROR
        return []

    # Applying custom colors
    df_style = df.style.apply(apply_status_color, axis=1)

    # Generate HTML content for the report using to_html() instead of render()
    html_content = df_style.to_html()

    # Add CSS to style the HTML report
    html_template = f"""
    <html>
    <head>
        <style>
            body {{
                font-family: Arial, sans-serif;
                margin: 20px;
            }}
            table {{
                width: 100%;
                border-collapse: collapse;
                margin-top: 20px;
                text-align: left;
            }}
            th, td {{
                padding: 12px;
                border: 1px solid #ddd;
            }}
            th {{
                background-color: #f4f4f4;
            }}
            tr:nth-child(even) {{
                background-color: #f9f9f9;
            }}
            .reason {{
                white-space: pre-wrap;
                word-wrap: break-word;
            }}
        </style>
    </head>
    <body>
        <h2>Unit Test Results</h2>
        <p>Test run date: {datetime.now().isoformat()}</p>
        {html_content}
    </body>
    </html>
    """

    # Save the HTML content to the output file
    with open(output_file, "w") as f:
        f.write(html_template)

    print(f"Test results stored in {output_file}")



if __name__ == "__main__":
    test_folder = "./tests"
    import os
    print(os.listdir(test_folder))

    # Execute tests and measure coverage automatically
    discover_and_run_tests(test_folder)
 


