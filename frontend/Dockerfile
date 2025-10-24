FROM node:18-alpine

# Set working directory
WORKDIR /app

# Copy package files and install dependencies
COPY package*.json ./
RUN npm install

# Copy the rest of the frontend
COPY . .

# Expose the same port your Vite app uses
EXPOSE 8080

# Run the development server, binding to all interfaces
CMD ["npm", "run", "dev", "--", "--host"]
