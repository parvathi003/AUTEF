import os
import shutil

def delete_files_and_folders():
    base_dir = r"D:\Sai\EnhanceUnitTesting"
    
    files_to_delete = [
        "test_results.html", "test_results.json",
        "coverage_report.txt", "coverage_report1.txt", "coverage_report2.txt",
        "fixed_testcaseresults.html", "fixed_testcaseresults.json",
        "results.html", "results.json",
        "improved_testcaseresults.html", "improved_testcaseresults.json",
        "mutation_report_before.yaml", "mutation_report_after.yaml",
        ".coverage"  # Add coverage database file
    ]
    
    directories_to_clear = [
        "source_files", "tests", 
        "coverage_html_report", "coverage_html1_report",
        "mutation_coverage", "mutation_coverage_before", "mutation_coverage_after","backup_files"
        ".cache", "__pycache__"  # Add Python cache directories
    ]
    
    # Delete files
    for file in files_to_delete:
        file_path = os.path.join(base_dir, file)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"Deleted file: {file_path}")
        except Exception as e:
            print(f"Error deleting file {file_path}: {e}")
    
    # Delete directories and their contents
    for directory in directories_to_clear:
        dir_path = os.path.join(base_dir, directory)
        try:
            if os.path.exists(dir_path):
                shutil.rmtree(dir_path)
                print(f"Deleted directory and contents: {dir_path}")
        except Exception as e:
            print(f"Error deleting directory {dir_path}: {e}")

    # Clean up Python cache in src directory
    src_dir = os.path.join(base_dir, "src")
    for root, dirs, files in os.walk(src_dir):
        if "__pycache__" in dirs:
            cache_dir = os.path.join(root, "__pycache__")
            try:
                shutil.rmtree(cache_dir)
                print(f"Deleted cache directory: {cache_dir}")
            except Exception as e:
                print(f"Error deleting cache directory {cache_dir}: {e}")

if __name__ == "__main__":
    delete_files_and_folders()