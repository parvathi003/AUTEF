# Function to generate prompt for creating unit tests
# Define language test frameworks
LANGUAGE_TEST_FRAMEWORKS = {
    "python": "unittest",
    "java": "JUnit",
    "javascript": "Mocha",
    "csharp": "xUnit",
    "ruby": "RSpec"
}

def get_prompt(code_chunk, file_name, language="python", prompt_type="functionality", shot_type="zero_shot"):
    module_name = file_name.replace(".py", "")  # Extract module name from file name
    
    """Generate a prompt for the specified code chunk and language."""
    few_shot_examples = {
        "python": """
        import unittest
        from example import Calculator

        class TestCalculator(unittest.TestCase):
            def test_add(self):
                calc = Calculator()
                self.assertEqual(calc.add(2, 3), 5)

        if __name__ == "__main__":
            unittest.main()
        """,
        "java": """
        import org.junit.Test;
        import static org.junit.Assert.*;

        public class CalculatorTest {
            @Test
            public void testAdd() {
                Calculator calc = new Calculator();
                assertEquals(5, calc.add(2, 3));
            }
        }
        """
    }

    test_framework = LANGUAGE_TEST_FRAMEWORKS.get(language, "unittest")
    examples = few_shot_examples.get(language, "")

    return f"""
    Create a comprehensive, edge-case-focused unit test suite for the following {language} class or functions:
    {code_chunk}

    **Requirements:**
    
    1. Import 'unittest' module and necessary dependencies for all test classes
        -import unittest 
    2. **Ensure `sys.path` is updated before importing modules**  
       - Dynamically determine the 'source_files' directory before importing other modules.
       - Update `sys.path` before attempting imports.
       - Example:
         ```python
         import sys
         import os

         current_dir = os.path.dirname(os.path.abspath(__file__))
         source_files_root = current_dir.replace(os.path.sep + "tests", os.path.sep + "source_files")

         def add_parent_directories_to_sys_path(path):
             while path not in sys.path and os.path.isdir(path):
                 sys.path.insert(0, path)
                 path = os.path.dirname(path)  # Move up one level
                 if path == os.path.dirname(path):  # Stop at root
                     break

         add_parent_directories_to_sys_path(source_files_root)
         ```

    
    3.**Import the module dynamically based on the file name**  
       ```python
       from {module_name} import *
       ```
    
    4. **Implement a test class using `{test_framework}`**  
       -import unittest  first before inheriting test class from 'unittest.TestCase'
       - The test class should inherit from `unittest.TestCase` for Python.
       -import unittest 
       - Include a `setUp()` method to reset any class-level counters before each test.

    5. **Ensure test coverage for all possible edge cases, including:**
       - Valid inputs.
       - Invalid inputs (e.g., wrong data types, out-of-range values).
       - Boundary values (e.g., min/max inputs).
       - Unexpected scenarios (e.g., empty inputs, null values).
       - Ensuring invalid IDs (e.g., zero or negative IDs) raise `ValueError`.

    6. **Handle Abstract Classes Properly**  
       - If the file contains an abstract class:
         ```python
         from abc import ABC, abstractmethod
         from typing import List, Optional
         ```
       - Use a mock subclass to implement abstract methods during testing.
       - Example:
         ```python
         class MockExample(AbstractExample):
             def required_method(self):
                 return "Mocked Result"
         ```

    7. **Mock External Dependencies**  
       - Patch external dependencies such as `settings` and `re`:
         ```python
         from unittest.mock import MagicMock
         settings.PASSWORD_MIN_LENGTH = 8
         ```

    8. **Ensure proper patching in unit tests**
       - If a method is imported as `from random import randint`, patch it as `random.randint`.

    9. **Provide Clear Documentation and Comments**  
       - Each test case should have a docstring or comment explaining its purpose.
       - Example:
         ```python
         def test_invalid_id_negative(self):
             \"\"\"Test that a negative ID raises ValueError.\"\"\"
             with self.assertRaises(ValueError):
                 Order("Peach", 2, id=-5)
         ```

    10. **Verify Exceptions and Edge Cases Properly**  
       - Ensure that exceptions are explicitly tested.
       - Example:
         ```python
         repository.find_by_id = MagicMock(side_effect=ValueError("ID must be a positive integer"))
         with self.assertRaises(ValueError):
             repository.find_by_id(-1)
         ```

    11. **Ensure Each Edge Case Has a Separate Test Case**  
        - Avoid combining multiple edge cases into a single test method.

    12.**Verify String Representation Matches the Actual Implementation**  
    - Ensure that expected string formatting correctly reflects the class implementation.  
    - Construct the expected string explicitly using the class attributes, not just `str(instance)`.  
    - Example for a `__str__` method in any class:
      ```python
      instance = MyClass(arg1, arg2)  # Replace with actual constructor params
      expected_str = f"MyClass(arg1={{instance.arg1}}, arg2={{instance.arg2}})"  # Ensure correct format
      self.assertEqual(str(instance), expected_str)
      ```  

    13. **Follow Coding Standards and Formatting**  
        - Use appropriate test framework conventions.
        - Ensure the generated code is PEP 8 compliant (for Python).

    14. **Include a Main Execution Block for Running Tests**  
        - Add the following block to ensure standalone execution:
        ```python
        if __name__ == "__main__":
            unittest.main()
        ```

    15. **Ensure the Output is a Fully Functional, Executable Test Script**  
        - Do not include any markdown syntax, such as ``` or language-specific markers (like `python`)
        -Remove all  lines starting with triple backticks ```
        -The final output should be an executable test file that can be run without modifications.

    **Example Test File:**
    {examples}
    """

def generate_branch_coverage_prompt(coverage, file_name, missing_statements, missing_branches,source_content, test_file_content):
    prompt = (
        f"Generate missing test cases for the file {file_name}.\n"
        f"Current test coverage is {coverage}%. The following branches are missing coverage:\n"
        f"- Missing Statements: {missing_statements}\n"
        f"- Missing Branches: {missing_branches}\n\n"
    
        f"--- Source Code ---\n{source_content}\n\n"
        f"--- Existing Test Cases ---\n{test_file_content}\n\n"

        f"### Objective: Maximize Branch Coverage\n"
        f"- Identify **only uncovered execution paths** in all control structures (`if`, `elif`, `else`, `for`, `while`, `try-except`).\n"
        f"- **Do NOT generate test cases that already exist in `test_file_content`.**\n"
        f"- **Focus ONLY on the missing branches listed above.**\n"
        f"- **Each test function must target a specific missing condition.**\n\n"

        f"### Constraints:\n"
        f"- **No Duplication:** Ensure new test cases do not repeat existing ones.\n"
        f"- **Do NOT generate class headers (`class TestXYZ`) or import statements.**\n"
        f"- **Do NOT include `if __name__ == \"__main__\": unittest.main()`**\n"
        f"- **Valid Python Tests Only:**\n"
        f"  - Each function must start with `def test_...`.\n"
        f"  - Each function must include `self` as the first parameter.\n"
        f"  - Each function must be properly formatted.\n"
        f"- **Do NOT include any markdown formatting such as ``` or ```python **\n"
        f"- **Each test function should have a descriptive name based on the missing branch.**\n"
        f"- **Assume existing setup: Do not redefine class instances if already initialized in `test_file_content`.**\n\n"

        f"### Test Coverage Strategy:\n"
        f"For each missing branch, generate test cases that cover all logical scenarios:\n"
        f"- If `if A and B:`, ensure tests for:\n"
        f"  - (A=True, B=True), (A=True, B=False), (A=False, B=True), (A=False, B=False)\n"
        f"- If `if A or B:`, ensure tests for:\n"
        f"  - (A=True, B=True), (A=True, B=False), (A=False, B=True), (A=False, B=False)\n"
        f"- Ensure **boundary cases**, **edge cases**, and **invalid inputs** are covered where relevant.\n"
    
        f"### Expected Output Format:\n"
        f"- **Only the missing test functions** without any extra text, instructions, or explanations.\n"
        f"- Do not include any markdown formatting such as ``` or ```python \n"
        f" -**Each test must be a properly formatted Python function starting with `def test_...`\n "
        f"- **Each test method should have a descriptive name and include the 'self' parameter. (e.g. 'def test_example(self):')**\n"
        )
    return prompt

def build_fix_prompt(source_code, test_function_code, error_reason):
    prompt = [
                 {
                    "role": "system",
                    "content": (
                        "You are a Python expert. You are given a source file, a failing test function that tests it, "
                        "and an error message. Fix the test function. Do not return explanations or formatting — just return the corrected test function code."
                        )
                },
                {
                    "role": "user",
                    "content": f""" Source Code:{source_code}
                                    Broken Test Function:{test_function_code}
                                    Error Message:{error_reason}
                                    ### Expected Output Format:
                                     -Only the updated functions** without any extra text, instructions, or explanations.
                                     -Do not include any markdown formatting such as ``` or ```python
                                     -Do NOT generate class headers (`class TestXYZ`) or import statements.
                                     -Each test must be a properly formatted Python function starting with `def test_...`
                                     -Each test method should have a descriptive name and include the 'self' parameter. (e.g. 'def test_example(self):')
                                """
                 }
             ]
    return prompt


def generate_mutation_prompt(source_code: str, original_test_code: str, mutation_diff: str) -> str:
    """
    Generates a prompt for the LLM to create test cases that catch the given mutation.
    
    Args:
        source_code (str): The original source code
        original_test_code (str): The existing test code
        mutation_diff (str): The mutation changes made
        strategy (str): The mutation strategy being applied (arithmetic, comparison, logical, boundary)
    """
    strategy = 'comparison'
    # Strategy-specific guidance
    strategy_guidance = {
        'arithmetic': "Test arithmetic operations (+,-,*,/) and edge cases (0, negative numbers)",
        'comparison': "Test comparison operators (==,!=,<,>,<=,>=) and boundary conditions",
        'logical': "Test logical operations (and,or,not) with boolean combinations",
        'boundary': "Test boundary values and off-by-one scenarios"
    }


    prompt = [
        {
            "role": "system",
            "content": (
                "You are a Python testing expert using the `unittest` framework. Your job is to improve the test code "
                "so it catches a mutation in the source code. A mutation is a small change (diff) in the logic. "
                f"Currently focusing on {strategy} mutations: {strategy_guidance.get(strategy, '')}. "
                "Your test code should fail if the mutation is present. "
                "Fix the test function. Do not return explanations or formatting — just return the corrected test function code."
            )
        },
        {
            "role": "user",
            "content": (
                f"Source Code:\n{source_code}\n\n"
                f"Test Code:\n{original_test_code}\n\n"
                f"Mutation Diff:\n{mutation_diff}\n\n"
                "### Expected Output Format:\n"
                "- Only the updated functions, without any extra text, instructions, or explanations.\n"
                "- Do not include any markdown formatting such as ``` or ```python.\n"
                "- Do NOT generate class headers (e.g., `class TestXYZ`) or import statements.\n"
                "- Each test must be a properly formatted Python function starting with `def test_...`\n"
                "- Each test method should have a descriptive name and include the 'self' parameter. (e.g., `def test_example(self):`)\n"
                f"- Focus on {strategy} mutations and their edge cases.\n"
            )
        }
    ]

    return prompt