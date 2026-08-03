import difflib
import ast
import inspect
import streamlit as st
import os
def apply_specific_mutation(source_code: str, line_number: int, mutator_type: str) -> tuple:
    """
    Applies a specific mutation to the source code at given line number.
    Returns tuple: (mutated_code, mutation_details)
    """
    lines = source_code.split('\n')
    original_line = lines[line_number - 1]
    mutated_line = original_line
    
    # Implement mutation rules based on mutator type
    if mutator_type == 'ArithmeticOperatorReplacement':
        replacements = {'+': '-', '*': '/', '-': '+', '/': '*'}
        for op, replacement in replacements.items():
            if op in original_line:
                mutated_line = original_line.replace(op, replacement)
                break
                
    elif mutator_type == 'ComparisonOperatorReplacement':
        replacements = {'>': '<', '<': '>=', '==': '!='}
        for op, replacement in replacements.items():
            if op in original_line:
                mutated_line = original_line.replace(op, replacement)
                break
                
    elif mutator_type == 'ConstantReplacement':
        if 'True' in original_line:
            mutated_line = original_line.replace('True', 'False')
        elif 'False' in original_line:
            mutated_line = original_line.replace('False', 'True')
        elif any(char.isdigit() for char in original_line):
            mutated_line = original_line.replace('0', '1').replace('1', '0')

    # Apply mutation
    lines[line_number - 1] = mutated_line
    mutated_code = '\n'.join(lines)
    
    # Generate diff
    diff = difflib.unified_diff(
        source_code.splitlines(),
        mutated_code.splitlines(),
        lineterm='',
        fromfile='original',
        tofile='mutated'
    )
    
    return mutated_code, {
        'original_line': original_line,
        'mutated_line': mutated_line,
        'diff': '\n'.join(diff),
        'line_number': line_number,
        'mutator_type': mutator_type
    }


def apply_mutation(source_code: str) -> tuple[str, str]:
    """
    Apply a simple mutation to the source code.
    Example: Replace '==' with '!=' to simulate a mutation.

    Returns:
        mutated_code (str): The mutated version of the source code.
        mutation_diff (str): Unified diff showing changes made.
    """
    mutated_code = source_code.replace("<", "<=")

    original_lines = source_code.splitlines(keepends=True)
    mutated_lines = mutated_code.splitlines(keepends=True)

    mutation_diff = ''.join(
        difflib.unified_diff(original_lines, mutated_lines, fromfile='original.py', tofile='mutated.py')
    )

    return mutated_code, mutation_diff
def display_mutation_results(stats, report_html_path):
    """Display mutation testing results in a formatted way"""
    st.subheader("📊 Mutation Test Results")
    
    # Create columns for layout
    col1, col2 = st.columns([1, 1])
    
    # Add metrics in first column
    with col1:
        st.metric(
            label="🎯 Mutation Score",
            value=f"{stats['mutation_score']:.1f}%",
            delta=f"{stats['killed']} killed mutants"
        )
    
    # Add progress in second column
    with col2:
        progress = stats['killed'] / stats['total'] if stats['total'] > 0 else 0
        st.progress(progress)
        
    # Create results table
    st.markdown("""
    | Metric | Value |
    |--------|-------|
    | 🎯 **Mutation Score** | {mutation_score:.1f}% |
    | ✅ **Killed Mutants** | {killed} |
    | ❌ **Survived Mutants** | {survived} |
    | 📊 **Total Mutants** | {total} |
    """.format(
        mutation_score=stats['mutation_score'],
        killed=stats['killed'],
        survived=stats['survived'],
        total=stats['total']
    ))
    
    # Show quality indicator
    quality_score = stats['mutation_score']
    if quality_score >= 80:
        st.success("🌟 Excellent mutation coverage!")
    elif quality_score >= 60:
        st.warning("⚠️ Good coverage, but room for improvement")
    else:
        st.error("❗ Coverage needs significant improvement")
    
    # Display report link
    st.markdown("### 📊 View Full Report")
    if os.path.exists(report_html_path):
        st.markdown(
            f'<a href="file:///{report_html_path}" target="_blank">'
            '🔍 Click here to view detailed mutation report</a>', 
            unsafe_allow_html=True
        )
    else:
        st.warning("⚠️ Mutation report not generated yet at: " + report_html_path)
        
    # Show improvement suggestions without using expander
    if stats['survived'] > 0:
        st.markdown("### 💡 Improvement Suggestions")
        st.info("""
        - Add tests for the {survived} surviving mutants
        - Focus on edge cases and boundary conditions
        - Consider adding negative test scenarios
        - Add assertions for unexpected behaviors
        - Test boundary conditions thoroughly
        """.format(survived=stats['survived']))
    