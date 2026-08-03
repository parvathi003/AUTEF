import sys
import os
from pathlib import Path

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

from agenticapp.utils import file_utils, mutationutils
from agenticapp.agents.TestGenerationAgent import process_file
from agenticapp.agents.TestExecutionAgent import discover_and_run_tests
from agenticapp.agents.CoverageAgent import measure_coverage, display_coverage_report, generate_and_update_tests, measure_coverage_with_cli
from agenticapp.agents.AutoFixingAgent import fix_failing_tests
from agenticapp.utils.file_utils import create_output_folder, extract_zip_file, clear_directories,backup_modified_files
from agenticapp.agents.MutationTestAgent import get_mutation_coverage, run_mutation_tests
from agenticapp.utils.mutationutils import display_mutation_results

# Configure logging
logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# Base paths
PROJECT_ROOT = os.path.abspath(r"D:\Sai\EnhanceUnitTesting")
SOURCE_FOLDER_PATH = os.path.join(PROJECT_ROOT, "source_files")
TEST_FOLDER_PATH = os.path.join(PROJECT_ROOT, "tests")

# Results and reports paths
RESULTS_FILE_PATH = os.path.join(PROJECT_ROOT, "results.html")
FIXED_TESTCASERESULTS_PATH = os.path.join(PROJECT_ROOT, "fixed_testcaseresults.html")
IMPROVED_TESTCASERESULTS_PATH = os.path.join(PROJECT_ROOT, "improved_testcaseresults.html")

# Coverage report paths
COVERAGE_REPORT_PATH = os.path.join(PROJECT_ROOT, "coverage_report.txt")
IMPROVED_COVERAGE_REPORT_PATH = os.path.join(PROJECT_ROOT, "coverage_report1.txt")
FIXED_COVERAGE_REPORT_PATH = os.path.join(PROJECT_ROOT, "coverage_report2.txt")

# Cache and results paths
CACHE_PATH = os.path.join(PROJECT_ROOT, ".cache")
TEST_RESULTS_FILE = os.path.join(PROJECT_ROOT, "results.json")
IMPROVED_TEST_RESULTS_FILE = os.path.join(PROJECT_ROOT, "improved_testcaseresults.json")

# Mutation test configurations
APP_NAME = "InsuranceApp_Modified"
MUTATION_TEST_FOLDER_PATH = os.path.join(TEST_FOLDER_PATH, APP_NAME)
MUTATION_SOURCE_FOLDER_PATH = os.path.join(SOURCE_FOLDER_PATH, APP_NAME)

def get_mutation_report_path(stage='before'):
    """Get stage-specific mutation report path"""
    report_dir = os.path.join(PROJECT_ROOT, f"mutation_coverage_{stage}")
    os.makedirs(report_dir, exist_ok=True)
    return os.path.join(report_dir, "index.html")

# Module paths for mutation testing
target_module = f"source_files.{APP_NAME}"
test_module = f"tests.{APP_NAME}"

def ensure_directories():
    """Ensure all required directories exist"""
    directories = [
        SOURCE_FOLDER_PATH,
        TEST_FOLDER_PATH,
        CACHE_PATH,
        os.path.dirname(get_mutation_report_path('before')),
        os.path.dirname(get_mutation_report_path('after'))
    ]
    for directory in directories:
        os.makedirs(directory, exist_ok=True)
        logger.info(f"Ensured directory exists: {directory}")

def initialize_session_state():
    """Initialize all session state variables"""
    initial_states = {
        "test_generated": False,
        "test_fixed": False,
        "coverage_improvement": False,
        "mutation_test": False,
        "has_improved_mutation_coverage": False,
        "tests_generated": False,
        "final_mutation_measured": False,
        "mutation_improvement_started": False,
        "final_mutation_coverage":False,
        "files_backed_up": False,
        "initial_stats": {
            "mutation_score": 0.0,
            "killed": 0,
            "survived": 0,
                                    "total": 0
        },
        "final_stats": {
            "mutation_score": 0.0,
            "killed": 0,
            "survived": 0,
            "total": 0
        },
        "mutation_stats": {
            "mutation_score": 0.0,
            "killed": 0,
            "survived": 0,
            "total": 0
        }
    }
    for key, value in initial_states.items():
        if key not in st.session_state:
            st.session_state[key] = value

def process_source_files(source_folder, test_folder):
    """Process each Python file in the source folder and its subfolders."""
    try:
        for root, _, files in os.walk(source_folder):
            for file in files:
                if file.endswith(".py") and file != "__init__.py":
                    file_path = os.path.join(root, file)
                    logger.info(f"Processing file: {file_path}")
                    process_file(file_path, output_folder=test_folder, language="python", shot_type="few_shot")
    except Exception as e:
        logger.error(f"Error processing source files: {str(e)}")
        st.error(f"❌ Error processing source files: {str(e)}")

def copy_source_files(source_folder, force=False):
    """Copy source files only if necessary"""
    try:
        # Only copy if directory doesn't exist or force is True
        if not os.path.exists(SOURCE_FOLDER_PATH) or force:
            if os.path.exists(SOURCE_FOLDER_PATH):
                logger.info("Removing existing source files directory")
                shutil.rmtree(SOURCE_FOLDER_PATH)
            
            logger.info(f"Copying source files from {source_folder}")
            shutil.copytree(source_folder, SOURCE_FOLDER_PATH)
            logger.info(f"Source files copied to: {SOURCE_FOLDER_PATH}")
            return True
        else:
            logger.info(f"Using existing files in: {SOURCE_FOLDER_PATH}")
            return True
    except Exception as e:
        logger.error(f"Error copying source files: {str(e)}")
        st.error(f"❌ Error copying source files: {str(e)}")
        return False

def handle_test_generation(test_directory):
    """Handle the test generation process"""
    try:
        create_output_folder(test_directory)
        process_source_files(SOURCE_FOLDER_PATH, TEST_FOLDER_PATH)
        discover_and_run_tests(TEST_FOLDER_PATH, TEST_RESULTS_FILE, RESULTS_FILE_PATH)
        measure_coverage(TEST_FOLDER_PATH, COVERAGE_REPORT_PATH)
        st.session_state.test_generated = True
        logger.info("Test generation completed successfully")
    except Exception as e:
        logger.error(f"Error in test generation: {str(e)}")
        st.error(f"❌ Error generating tests: {str(e)}")

def handle_coverage_improvement():
    """Handle the coverage improvement process"""
    try:
        generate_and_update_tests(COVERAGE_REPORT_PATH, TEST_FOLDER_PATH)
        discover_and_run_tests(TEST_FOLDER_PATH, IMPROVED_TEST_RESULTS_FILE, IMPROVED_TESTCASERESULTS_PATH)
        measure_coverage_with_cli(TEST_FOLDER_PATH, IMPROVED_COVERAGE_REPORT_PATH)
        st.session_state.coverage_improvement = True
        logger.info("Coverage improvement completed successfully")
    except Exception as e:
        logger.error(f"Error improving coverage: {str(e)}")
        st.error(f"❌ Error improving coverage: {str(e)}")

def handle_test_fixing():
    """Handle the test fixing process"""
    try:
        fix_failing_tests(IMPROVED_TEST_RESULTS_FILE)
        discover_and_run_tests(TEST_FOLDER_PATH, "fixed_testcaseresults.json", FIXED_TESTCASERESULTS_PATH)
        measure_coverage_with_cli(TEST_FOLDER_PATH, FIXED_COVERAGE_REPORT_PATH)
        st.session_state.test_fixed = True
        logger.info("Test fixing completed successfully")
    except Exception as e:
        logger.error(f"Error fixing tests: {str(e)}")
        st.error(f"❌ Error fixing tests: {str(e)}")
def handle_mutation_testing(stage='before'):
    """Handle mutation testing process"""
    try:
        report_dir = os.path.join(PROJECT_ROOT, f"mutation_coverage_{stage}")
        os.makedirs(report_dir, exist_ok=True)
        
        if stage == 'before':
            logger.info("Starting initial mutation testing")
            coverage, stats = get_mutation_coverage(PROJECT_ROOT, target_module, test_module, stage)
            if stats:
                st.session_state.initial_stats = stats.copy()
                st.session_state.mutation_stats = stats.copy()
                st.session_state.mutation_test = True
                logger.info("Initial mutation testing completed successfully")
                
        elif stage == 'after':
            logger.info("Starting final mutation testing with modified files")
            logger.info("Using existing modified files for testing")
            
            coverage, stats = get_mutation_coverage(PROJECT_ROOT, target_module, test_module, stage)
            if stats:
                st.session_state.final_stats = stats.copy()
                st.session_state.mutation_stats = stats.copy()
                st.session_state.final_mutation_measured = True
                logger.info("Final mutation testing completed successfully")
                
        return coverage, stats
        
    except Exception as e:
        logger.error(f"Error in mutation testing ({stage}): {str(e)}")
        st.error(f"❌ Error in mutation testing: {str(e)}")
        return None, None
def display_results_link(file_path, title):
    """Display a clickable link to view results"""
    abs_path = os.path.abspath(file_path).replace("\\", "/")
    st.markdown(f'<a href="file:///{abs_path}" target="_blank">{title}</a>', unsafe_allow_html=True)

def main():
    #st.title('🧪  Enhance Unit Testing Using LLMs')
    st.markdown("""
        <h1 style='text-align: center; white-space: nowrap;'>
            🧪 Agentic UnitTest Enhancement<br> Framework(AUTEF)
        </h1>
    """, unsafe_allow_html=True)
    initialize_session_state()
    ensure_directories()

    # File upload section
    uploaded_zip = st.file_uploader("Choose a zip file containing the source folder", type=["zip"])
    if uploaded_zip:
        source_folder = extract_zip_file(uploaded_zip)
        if copy_source_files(source_folder, force=True):
            st.success("✅ Source files uploaded successfully")
        else:
            st.error("❌ Failed to copy source files")
            return
    
        test_directory = os.path.join(TEST_FOLDER_PATH, APP_NAME)
        os.makedirs(test_directory, exist_ok=True)

        if st.button('🚀 Generate Unit TestCases'):
            handle_test_generation(test_directory)
            st.rerun()
    # Ensure directories exist without copying
    ensure_directories()
    # Display test results
    if st.session_state.test_generated:
        st.subheader("📄 Initial Test Results")
        display_results_link(RESULTS_FILE_PATH, "View Test Results")
        df = display_coverage_report(COVERAGE_REPORT_PATH)
        st.subheader("📊 Test Coverage Report")
        st.dataframe(df)
        
        if st.button("🚀 Improve Coverage"):
            handle_coverage_improvement()
            st.rerun()

    # Display improved results
    if st.session_state.coverage_improvement:
        with st.expander("🔧 Improved Test Cases", expanded=True):
            st.subheader("📄 View Improved Results")
            display_results_link(IMPROVED_TESTCASERESULTS_PATH, "View Results")
            df = display_coverage_report(IMPROVED_COVERAGE_REPORT_PATH)
            st.subheader("📊 Improved Coverage Report")
            st.dataframe(df)
            
            if st.button('🚀 Fix Unit TestCases'):
                handle_test_fixing()
                st.rerun()

    # Display fixed results and mutation testing
    if st.session_state.test_fixed:
        with st.expander("🔧 Fixed Test Cases", expanded=True):
            st.subheader("📄 View Fixed Results")
            display_results_link(FIXED_TESTCASERESULTS_PATH, "View Results")
            df = display_coverage_report(FIXED_COVERAGE_REPORT_PATH)
            st.subheader("📊 Final Coverage Report")
            st.dataframe(df)
            
            if not st.session_state.mutation_test:
                if st.button('🚀 Start Mutation Testing'):
                    coverage, stats = handle_mutation_testing('before')
                    if stats:
                        st.session_state.mutation_test = True
                        st.rerun()

  # Show mutation testing results and improvement workflow
    if st.session_state.mutation_test:
        with st.expander("🔧 Mutation Testing Results", expanded=True):
            # Always show initial results
            st.subheader("📊 Initial Mutation Coverage")
            initial_mutation_report = get_mutation_report_path('before')
            display_mutation_results(st.session_state.initial_stats, initial_mutation_report)
            
            # Show test generation button if improvement not started
            if not st.session_state.mutation_improvement_started:
                if st.button('🚀 Generate Additional Test Cases'):
                    run_mutation_tests(MUTATION_TEST_FOLDER_PATH, MUTATION_SOURCE_FOLDER_PATH)
                    st.session_state.tests_generated = True
                    st.session_state.mutation_improvement_started = True
                    st.info("✅ Additional test cases have been generated")
                    st.info("💡 Please review and update the source code based on the new test cases")
                    st.rerun()
            
            # Show manual update section after test generation
            if st.session_state.tests_generated and not st.session_state.final_mutation_measured:
                st.markdown("---")
                st.subheader("📝 Manual Code Updates")
                st.info("1. Review generated test cases in the test folder")
                st.info("2. Update source code to handle new test cases")
                st.warning("3. Save all code changes before measuring")
                if st.button('🚀 Measure Final Mutation Coverage'):
                    st.session_state.final_mutation_measured = True
                    st.rerun()
                 
            # Show final results after measurement
            if st.session_state.final_mutation_measured:
                st.markdown("---")  # Add separator
                st.subheader("📊 Final Mutation Results")
                final_mutation_report = get_mutation_report_path('after')
                abs_path = os.path.abspath(final_mutation_report).replace("\\", "/")
                st.markdown(f'<a href="file:///{abs_path}" target="_blank">📋 View Detailed Mutation Report</a>', unsafe_allow_html=True )
                if st.button("🚀 Deploy", type="primary"):
                    st.success("✅ Deployment initiated successfully!")
                    st.balloons()
                
if __name__ == "__main__":
    main()