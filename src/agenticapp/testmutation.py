from pathlib import Path
import sys
# Add src directory to Python path
src_path = str(Path(__file__).parent.parent)
if src_path not in sys.path:
    sys.path.insert(0, src_path)

import logging
import os
import streamlit as st
import shutil

# Group related imports
from agenticapp.agents import (
    TestGenerationAgent, 
    TestExecutionAgent,
    CoverageAgent,
    AutoFixingAgent,
    MutationTestAgent
)


import os
import re
import yaml  # Changed to use pyyaml for YAML support
import subprocess
from autogen import AssistantAgent
from autogen.oai.openai_utils import config_list_from_json
from dotenv import load_dotenv
from agenticapp.template_prompts import generate_mutation_prompt
from agenticapp.utils.mutationutils import apply_mutation
from agenticapp.agents.CoverageAgent import update_test_files
import sys
import traceback
# Load environment variables
load_dotenv()

# Load config from JSON and replace environment variable placeholder
config_path = r"D:\\Sai\\EnhanceUnitTesting\\src\\agenticapp\\OAI_CONFIG_LIST.json"
config_list = config_list_from_json(config_path)

for config in config_list:
    if "api_key" in config and config["api_key"] == "ENV_OPENAI_API_KEY":
        config["api_key"] = os.getenv("OPENAI_API_KEY")

# Create the mutation test assistant agent
mutationtest_agent = AssistantAgent(
    name="MutationTestAgent",
    llm_config={"config_list": config_list, "temperature": 0},
)

def locate_source_file_from_test(test_file_path, test_base_folder, source_base_folder):
    relative_path = os.path.relpath(test_file_path, test_base_folder)
    source_path = os.path.join(source_base_folder, relative_path)
    base_dir, filename = os.path.split(source_path)

    filename_core = re.sub(r'^test_?', '', filename)
    filename_core = re.sub(r'(_\d+)?\.py$', '.py', filename_core)
    
    final_source_path = os.path.join(base_dir, filename_core)
    return final_source_path if os.path.exists(final_source_path) else None

def extract_stats(report_path):
    """Parse mut.py's YAML report to get mutation stats."""
    stats = {
        "killed": 0,
        "survived": 0,
        "timeout": 0,
        "incompetent": 0,
        "total": 0,
        "mutation_score": 0.0
    }
    
    try:
        if not os.path.exists(report_path):
            print(f"⚠️ Report file not found: {report_path}")
            return stats
            
        # Add custom constructor to handle Python module tags
        def ignore_python_module(loader, suffix, node):
            return None  # Ignore !!python/module tags
            
        yaml.SafeLoader.add_multi_constructor('tag:yaml.org,2002:python/module', ignore_python_module)

        with open(report_path, 'r') as f:
            report = yaml.safe_load(f)
            
        # Handle different report formats (in YAML)
        mutations = report.get('mutations', [])
            
        for mut in mutations:
            status = mut.get('status', 'survived').lower()
            if status in stats:
                stats[status] += 1
            stats['total'] += 1
            
        stats['mutation_score'] = report.get('mutation_score', 0.0)
        
        return stats
        
    except yaml.YAMLError:
        print(f"⚠️ Invalid YAML in report: {report_path}")
        return stats
    except Exception as e:
        print(f"⚠️ Error parsing {report_path}: {str(e)}")
        return stats

def get_mutation_coverage(project_root, target_module, test_module, stage_name=''):
    """
    Runs mut.py mutation testing and returns coverage stats with stage-specific YAML report.
    """
    print(f"🔁 Running mutation testing ({stage_name})...")
    
    # Generate unique report name
    report_name = f'mutation_report_{stage_name}.yaml' if stage_name else 'mutation_report.yaml'
    report_path = os.path.join(project_root, report_name)
    # Generate HTML report directory with stage name
    html_report_dir = f"mutation_coverage_{stage_name}" if stage_name else "mutation_coverage"
    try:
        cmd = [
            sys.executable,  # This gets the current Python interpreter path,
            'c:/Users/sbask/anaconda3/Scripts/mut.py',
            '--target', target_module,
            '--unit-test', test_module,
            '--report', report_path,
            '--report-html', html_report_dir
        ]
        
        # Ensure report directory exists
        report_dir = os.path.dirname(report_path)
        html_report_path = os.path.join(project_root, html_report_dir)
        for dir_path in [report_dir, html_report_path]:
            if not os.path.exists(dir_path):
                os.makedirs(dir_path)
        
        # Run mutation test
        result = subprocess.run(
            cmd, 
            cwd=project_root, 
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8'
        )
        
        # Parse and calculate stats
        stats = extract_stats(report_path)
        relevant = stats['killed'] + stats['survived']
        coverage = (stats['killed'] / relevant * 100) if relevant else 0
        
        print(f"✅ Saved {stage_name} report to: {report_path}")
        return coverage, stats

    except subprocess.CalledProcessError as e:
        print(f"⚠️ Mutation testing failed: {e.stderr}")
        return 0, stats
    except Exception as e:
        print(f"⚠️ General error during mutation testing: {str(e)}")
        return 0, stats

def generate_mutation_test_for_file(test_file_path, test_base_folder, source_base_folder):
    source_path = locate_source_file_from_test(test_file_path, test_base_folder, source_base_folder)
    if not source_path:
        print(f"⚠️ Could not locate source file for {test_file_path}")
        return

    try:
        with open(source_path, 'r', encoding='utf-8') as f:
            original_source = f.read()

        with open(test_file_path, 'r', encoding='utf-8') as f:
            original_test = f.read()
       
        mutated_source, mutation_diff = apply_mutation(original_source)
        messages = generate_mutation_prompt(original_source, original_test, mutation_diff)
        
        response = mutationtest_agent.generate_oai_reply(messages)
        
        # Process generated test code
        generated_test_code = response[1].strip() if isinstance(response, tuple) else response.strip()
        
        update_test_files(test_file_path, generated_test_code)
        print(f"✅ Mutation test updated for: {test_file_path}")

    except Exception as e:
        print(f"⚠️ Error processing {test_file_path}: {str(e)}")

def run_mutation_tests(test_base_folder, source_base_folder):
    for root, _, files in os.walk(source_base_folder):
        for file in files:
            if file.endswith(".py") and file != "__init__.py":
                source_file_path = os.path.join(root, file)
                print(f"\n🔍 Processing source file: {source_file_path}")
                
                relative_path = os.path.relpath(source_file_path, source_base_folder)
                source_file_name = os.path.splitext(file)[0]
                test_folder = os.path.join(test_base_folder, os.path.dirname(relative_path))
                
                print(f"🔍 Looking for tests in: {test_folder}")
                
                if not os.path.exists(test_folder):
                    print(f"⚠️ Test folder not found for: {source_file_path}")
                    continue

                test_files = [
                    f for f in os.listdir(test_folder)
                    if re.match(fr'^test_?{re.escape(source_file_name)}(_\d+)?\.py$', f)
                ]
                
                print(f"🔍 Found matching test files: {test_files}")

                if test_files:
                    for test_file in test_files:
                        test_file_path = os.path.join(test_folder, test_file)
                        print("\n🚀 Processing test file:", test_file_path)
                        generate_mutation_test_for_file(test_file_path, test_base_folder, source_base_folder)
                else:
                    print(f"⚠️ Test file not found for: {source_file_path}")
    
    print("\n✅ Mutation test generation completed for all source files with tests.")

if __name__ == "__main__":
    APP_NAME = "InsuranceApp_Modified"
    
    # Path configurations
    TEST_FOLDER_PATH = os.path.join(r"D:\Sai\EnhanceUnitTesting\tests", APP_NAME)
    SOURCE_FOLDER_PATH = os.path.join(r"D:\Sai\EnhanceUnitTesting\source_files", APP_NAME)
    PROJECT_ROOT = r"D:\Sai\EnhanceUnitTesting"
    
    # Module paths for mut.py
    target_module = f"source_files.{APP_NAME}"
    test_module = f"tests.{APP_NAME}"

    print(f"\n📂 Selected App: {APP_NAME}")
    
    # Before mutation tests
    #print("📈 Measuring mutation coverage BEFORE mutation tests...")
    #coverage_before, stats_before = get_mutation_coverage(
        #PROJECT_ROOT, target_module, test_module, 'before'
    #)

    # Generate mutation tests
    #run_mutation_tests(TEST_FOLDER_PATH, SOURCE_FOLDER_PATH)

    # After mutation tests
    print("\n📈 Measuring mutation coverage AFTER mutation tests...")
    coverage_after, stats_after = get_mutation_coverage(
        PROJECT_ROOT, target_module, test_module, 'after'
    )

    # Enhanced reporting
    print("\n📊 Mutation Coverage Report")
    print(f"| Metric            | Before  | After   | Change  |")
    print(f"|-------------------|---------|---------|---------|")
    #print(f"| Killed Mutants    | {stats_before['killed']:7} | {stats_after['killed']:7} | {stats_after['killed'] - stats_before['killed']:7} |")
    #print(f"| Survived Mutants  | {stats_before['survived']:7} | {stats_after['survived']:7} | {stats_after['survived'] - stats_before['survived']:7} |")
    #print(f"| Total Mutants     | {stats_before['total']:7} | {stats_after['total']:7} | {stats_after['total'] - stats_before['total']:7} |")
    #print(f"| Mutation Score    | {stats_before['mutation_score']:7.1f}% | {stats_after['mutation_score']:7.1f}% | {stats_after['mutation_score'] - stats_before['mutation_score']:7.1f}% |")
    
    print("\n🔍 YAML reports available at:") 
    #print(f"- Before: {os.path.join(PROJECT_ROOT, 'mutation_report_before.yaml')}")
    print(f"- After:  {os.path.join(PROJECT_ROOT, 'mutation_report_after.yaml')}")
