FROM python:3.10.18

# Create and set the working directory
WORKDIR /usr/inlumen
RUN pip install --upgrade pip

# Copy requirements.txt. 
COPY ./requirements.txt ./requirements.txt

# Install Python dependencies from requirements.txt. 
# Install before copying the rest of the files to leverage Docker cache
# and avoid reinstalling dependencies if only the code changes.
RUN pip3 install -r requirements.txt

# Copy all files to the working directory in the container
COPY . ./

# Convert line endings for shell scripts and Python files to Unix format
RUN find . \( -name "*.sh" -o -name "*.py" \) -exec sed -i 's/\r$//' {} +

# Install Python dependencies from requirements.txt
# RUN pip3 install -r requirements.txt

# Expose the backend gateway API.
EXPOSE 5000

# Give execute permission to shell scripts
RUN chmod +x run_inlumen.sh

CMD ["python", "inlumen_api.py"]
