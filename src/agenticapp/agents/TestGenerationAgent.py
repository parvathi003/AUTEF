import autogen
import os
import logging
import re
from dotenv import load_dotenv
from utils.chunking_utils import chunk_code
from agenticapp.utils.file_utils import list_files, read_file, write_file, create_init_files
from template_prompts import get_prompt

# Load environment variables
load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    filename="app.log",
    filemode="w"
)
logger = logging.getLogger(__name__)

# Define AI Agent Configuration
config_path = r"D:\Sai\EnhanceUnitTesting\src\agenticapp\OAI_CONFIG_LIST.json"
config_list = autogen.config_list_from_json(config_path)

# Replace the placeholder with the actual API key from .env
for config in config_list:
    if "api_key" in config: 
        config["api_key"] = api_key

# Define Agents
generation_agent = autogen.AssistantAgent(
    name="TestGenerationAgent",
    llm_config={"config_list": config_list, "temperature": 0},
)


def clean_generated_code(test_code):
    patterns_to_remove = [r"TERMINATE"]
    for pattern in patterns_to_remove:
        test_code = re.sub(pattern, '', test_code, flags=re.MULTILINE)
    return test_code.strip()

def generate_tests(code_chunk, file_name,language="python",prompt_type="functionality", shot_type="few_shot"):
    try:
        module_name = os.path.splitext(file_name)[0]
        prompt = get_prompt(code_chunk, module_name,language, "functionality", shot_type)
        response = generation_agent.generate_oai_reply(messages=[{"role": "user", "content": prompt}])
        response_content = response[1].strip() if isinstance(response, tuple) else response.strip()
        return clean_generated_code(response_content)
    except Exception as e:
        logger.error(f"Error generating tests: {e}")
        return ""


def process_file(file_path, output_folder="tests", language="python", shot_type="few_shot"):
    try:
        code = read_file(file_path)
        code_chunks = chunk_code(code, max_chunk_size=512)
        source_folder = "source_files"
        relative_path = os.path.relpath(file_path, source_folder)
        base_name = os.path.splitext(os.path.basename(file_path))[0]
        test_subfolder = os.path.join(output_folder, os.path.dirname(relative_path))
        os.makedirs(test_subfolder, exist_ok=True)
        create_init_files(test_subfolder)
        
        for i, chunk in enumerate(code_chunks):
            output_file = os.path.join(test_subfolder, f"test_{base_name}_{i}.py")
            test_code = generate_tests(chunk, f"{base_name}.py",language, shot_type)
            write_file(output_file, test_code)
            
    except Exception as e:
        logger.error(f"Error processing file {file_path}: {e}")
