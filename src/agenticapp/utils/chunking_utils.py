# chunking_utils.py
import re

def chunk_code(code, max_chunk_size=512):
    """Split code into chunks of specified maximum size."""
    lines = code.splitlines()
    chunks, current_chunk = [], []

    for line in lines:
        if len(current_chunk) + len(line) > max_chunk_size:
            chunks.append("\n".join(current_chunk))
            current_chunk = []
        current_chunk.append(line)
    
    if current_chunk:
        chunks.append("\n".join(current_chunk))

    return chunks
