
import { toast } from 'sonner';
import { Node } from 'reactflow';
import { ChatbotConfig } from '@/services/chatbotService';

interface Message {
  role: 'system' | 'user' | 'assistant';
  content: string;
}

interface AzureAIResponse {
  choices: {
    message: {
      content: string;
    };
  }[];
}

export const callAzureAI = async (
  messages: Message[], 
  githubToken: string, 
  nodes?: Node[],
  config?: ChatbotConfig
): Promise<string> => {
  if (!githubToken) {
    toast.error('GitHub token is required', {
      description: 'Please enter your GitHub token in the settings',
    });
    return 'GitHub token is required to authenticate with Azure AI.';
  }

  if (!config) {
    toast.error('No chatbot configuration selected', {
      description: 'Please select or create a configuration before chatting',
    });
    return 'Chatbot configuration is required to generate responses.';
  }

  try {
    // Start with fresh message array
    let processedMessages = [...messages];

    // Always apply system prompt from configuration
    // First, remove any existing system messages
    processedMessages = processedMessages.filter(msg => msg.role !== 'system');
    
    // Then add the configuration's system prompt at the beginning
    processedMessages.unshift({
      role: 'system',
      content: config.system_prompt
    });
    
    // Process workflow nodes if available
    if (nodes && nodes.length > 0) {
      try {
        // Extract nodes by type
        const inputNodes = nodes.filter(node => node.data.type === 'input');
        const outputNodes = nodes.filter(node => node.data.type === 'output');
        const configNodes = nodes.filter(node => node.data.type === 'config');
        
        // Process input nodes - modify user messages if needed
        if (inputNodes.length > 0 && inputNodes[0].data.content) {
          // Find the last user message
          const userMessageIndex = processedMessages.map(msg => msg.role).lastIndexOf('user');
          if (userMessageIndex !== -1) {
            // Apply input template if it exists
            const inputTemplate = inputNodes[0].data.content;
            const userContent = processedMessages[userMessageIndex].content;
            
            if (inputTemplate.includes("{input}")) {
              // Replace {input} in template with user content
              processedMessages[userMessageIndex].content = inputTemplate.replace("{input}", userContent);
            }
          }
        }
      } catch (error) {
        console.error('Error processing workflow nodes:', error);
      }
    }

    console.log('Sending request with config:', { 
      model: config.model, 
      temperature: config.temperature, 
      messages: processedMessages
    });
    
    const response = await fetch('https://models.inference.ai.azure.com/chat/completions', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${githubToken}`,
      },
      body: JSON.stringify({
        messages: processedMessages,
        model: config.model,
        temperature: config.temperature,
        max_tokens: 4096,
        top_p: 1
      }),
    });

    if (!response.ok) {
      const errorData = await response.json();
      throw new Error(errorData.error?.message || 'Failed to get response from Azure AI');
    }

    const data = await response.json() as AzureAIResponse;
    return data.choices[0].message.content;
  } catch (error) {
    console.error('Error calling Azure AI:', error);
    toast.error('Azure AI API Error', {
      description: error instanceof Error ? error.message : 'Unknown error occurred',
    });
    return 'An error occurred while processing your request. Please check your GitHub token and try again.';
  }
};