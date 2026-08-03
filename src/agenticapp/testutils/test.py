import subprocess

cmd = [
    "c:/Users/sbask/anaconda3/python.exe",
    "c:/Users/sbask/anaconda3/Scripts/mut.py",
    "--target", "source_files.InsuranceApp_Modified",
    "--unit-test", "tests.InsuranceApp_Modified",
    "--report", r"D:\Sai\EnhanceUnitTesting\mutation_report.yaml"
]

#result = subprocess.run(cmd, capture_output=True, text=True)

result = subprocess.run(
            cmd, 
            check=True,
            capture_output=True,
            text=True,
            encoding='utf-8')
        
print("STDOUT:", result.stdout)
print("STDERR:", result.stderr)
