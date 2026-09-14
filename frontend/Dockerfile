FROM node:22-alpine@sha256:c610fcdfb1d5b4740dd70c284ed3cb16bb857e0f7166196e36a5501df7a3aa32

# Set working directory
WORKDIR /app

# Copy package files and install dependencies
COPY package*.json ./
RUN npm ci

# Copy the rest of the frontend
COPY . .

# Expose the same port your Vite app uses
EXPOSE 8080

# Run the development server, binding to all interfaces
CMD ["npm", "run", "dev", "--", "--host"]
