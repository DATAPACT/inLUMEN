
import { supabase } from "@/integrations/supabase/client";
import { toast } from "sonner";

export interface ChatbotConfig {
  id?: string;
  name: string;
  system_prompt: string;
  temperature: number;
  model: string;
}

export const fetchChatbotConfigs = async (): Promise<ChatbotConfig[]> => {
  try {
    const { data, error } = await supabase
      .from('chatbot_configurations')
      .select('*')
      .order('created_at', { ascending: false });
    
    if (error) throw error;
    return data || [];
  } catch (error) {
    console.error('Error fetching chatbot configurations:', error);
    toast.error('Failed to load configurations');
    return [];
  }
};

export const fetchChatbotConfig = async (id: string): Promise<ChatbotConfig | null> => {
  try {
    const { data, error } = await supabase
      .from('chatbot_configurations')
      .select('*')
      .eq('id', id)
      .single();
    
    if (error) throw error;
    return data;
  } catch (error) {
    console.error('Error fetching chatbot configuration:', error);
    toast.error('Failed to load configuration');
    return null;
  }
};

export const createChatbotConfig = async (config: ChatbotConfig): Promise<ChatbotConfig | null> => {
  try {
    // Ensure all required fields are present
    if (!config.name || !config.system_prompt || config.temperature === undefined || !config.model) {
      throw new Error('Missing required configuration fields');
    }

    const { data, error } = await supabase
      .from('chatbot_configurations')
      .insert(config)
      .select()
      .single();
    
    if (error) throw error;
    toast.success('Configuration saved successfully');
    return data;
  } catch (error) {
    console.error('Error creating chatbot configuration:', error);
    toast.error('Failed to save configuration');
    throw error; // Re-throw to allow proper error handling
  }
};

export const updateChatbotConfig = async (config: ChatbotConfig): Promise<ChatbotConfig | null> => {
  if (!config.id) return null;
  
  try {
    // Ensure all required fields are present
    if (!config.name || !config.system_prompt || config.temperature === undefined || !config.model) {
      throw new Error('Missing required configuration fields');
    }

    const { data, error } = await supabase
      .from('chatbot_configurations')
      .update({
        name: config.name,
        system_prompt: config.system_prompt,
        temperature: config.temperature,
        model: config.model,
        updated_at: new Date().toISOString()
      })
      .eq('id', config.id)
      .select()
      .single();
    
    if (error) throw error;
    toast.success('Configuration updated successfully');
    return data;
  } catch (error) {
    console.error('Error updating chatbot configuration:', error);
    toast.error('Failed to update configuration');
    throw error; // Re-throw to allow proper error handling
  }
};

export const deleteChatbotConfig = async (id: string): Promise<boolean> => {
  try {
    const { error } = await supabase
      .from('chatbot_configurations')
      .delete()
      .eq('id', id);
    
    if (error) throw error;
    toast.success('Configuration deleted successfully');
    return true;
  } catch (error) {
    console.error('Error deleting chatbot configuration:', error);
    toast.error('Failed to delete configuration');
    return false;
  }
};