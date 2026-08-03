import os
import shutil

def sync_test_directories():
    # Define source and target directories
    source_dir = r"C:\Users\sbask\OneDrive\Documents\tests"
    target_dir = r"D:\Sai\EnhanceUnitTesting\tests"

    try:
        # Delete all files in target directory if it exists
        if os.path.exists(target_dir):
            print(f"🗑️ Cleaning target directory: {target_dir}")
            shutil.rmtree(target_dir)
        
        # Create target directory if it doesn't exist
        os.makedirs(target_dir, exist_ok=True)
        
        # Copy all files from source to target
        print(f"📂 Copying files from: {source_dir}")
        print(f"📁 To: {target_dir}")
        shutil.copytree(source_dir, target_dir, dirs_exist_ok=True)
        
        print("✅ Directory sync completed successfully!")
        
    except Exception as e:
        print(f"❌ Error during directory sync: {str(e)}")

if __name__ == "__main__":
    sync_test_directories()