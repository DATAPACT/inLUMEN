FROM python:3.10.18
COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /uvx /bin/

# Create and set the working directory
WORKDIR /usr/inlumen

# Copy requirements.txt. 
COPY ./requirements.txt ./requirements.txt

# Install Python dependencies from requirements.txt. 
# Install before copying the rest of the files to leverage Docker cache
# and avoid reinstalling dependencies if only the code changes.
RUN --mount=type=cache,target=/root/.cache/uv uv pip install --system -r requirements.txt

# Copy all files to the working directory in the container
COPY . ./

# Convert line endings for shell scripts and Python files to Unix format
RUN find . \( -name "*.sh" -o -name "*.py" \) -exec sed -i 's/\r$//' {} +

# Expose the backend gateway API.
EXPOSE 5000

# Give execute permission to shell scripts
RUN chmod +x run_inlumen.sh

CMD ["python", "inlumen_api.py"]
