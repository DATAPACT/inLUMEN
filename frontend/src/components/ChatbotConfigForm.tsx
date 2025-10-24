
import React, { useState, useEffect } from 'react';
import { 
  Dialog, 
  DialogContent, 
  DialogHeader, 
  DialogTitle, 
  DialogDescription, 
  DialogFooter 
} from "@/components/ui/dialog";
import { 
  Form, 
  FormControl, 
  FormDescription, 
  FormField, 
  FormItem, 
  FormLabel, 
  FormMessage 
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import * as z from "zod";
import { Slider } from "@/components/ui/slider";
import { 
  ChatbotConfig, 
  createChatbotConfig, 
  updateChatbotConfig
} from "@/services/chatbotService";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { toast } from "sonner";

// Define form validation schema
const formSchema = z.object({
  name: z.string().min(1, "Configuration name is required"),
  system_prompt: z.string().min(1, "System prompt is required"),
  temperature: z.number().min(0).max(2),
  model: z.string().min(1, "Model selection is required"),
});

interface ChatbotConfigFormProps {
  isOpen: boolean;
  onClose: () => void;
  initialConfig?: ChatbotConfig;
  onConfigSaved: (config: ChatbotConfig) => void;
}

export function ChatbotConfigForm({ 
  isOpen, 
  onClose, 
  initialConfig, 
  onConfigSaved 
}: ChatbotConfigFormProps) {
  // Set up form with validation
  const form = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      name: initialConfig?.name || "New Configuration",
      system_prompt: initialConfig?.system_prompt || "You are a helpful assistant.",
      temperature: initialConfig?.temperature || 1.0,
      model: initialConfig?.model || "gpt-4o",
    },
  });

  // Update form values when initialConfig changes
  useEffect(() => {
    if (initialConfig) {
      form.reset({
        name: initialConfig.name,
        system_prompt: initialConfig.system_prompt,
        temperature: initialConfig.temperature,
        model: initialConfig.model,
      });
    } else {
      form.reset({
        name: "New Configuration",
        system_prompt: "You are a helpful assistant.",
        temperature: 1.0,
        model: "gpt-4o",
      });
    }
  }, [initialConfig, form]);

  // Handle form submission
  const onSubmit = async (values: z.infer<typeof formSchema>) => {
    try {
      // Create a complete config object with all required fields
      const configData: ChatbotConfig = {
        name: values.name,
        system_prompt: values.system_prompt,
        temperature: values.temperature,
        model: values.model,
      };
      
      // Add ID if we're updating an existing config
      if (initialConfig?.id) {
        configData.id = initialConfig.id;
      }

      // Save the configuration
      let savedConfig;
      if (initialConfig?.id) {
        savedConfig = await updateChatbotConfig(configData);
      } else {
        savedConfig = await createChatbotConfig(configData);
      }

      if (savedConfig) {
        // Only notify success and close if we got a valid response
        onConfigSaved(savedConfig);
        onClose();
      } else {
        // This shouldn't happen if the backend is working correctly
        throw new Error("Failed to save configuration");
      }
    } catch (error) {
      console.error("Error saving configuration:", error);
      toast.error("Failed to save configuration", {
        description: error instanceof Error ? error.message : "Unknown error occurred"
      });
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => {
      if (!open) onClose();
    }}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>
            {initialConfig ? "Edit Configuration" : "New Configuration"}
          </DialogTitle>
          <DialogDescription>
            Configure the AI parameters for your chatbot
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-6">
            <FormField
              control={form.control}
              name="name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Configuration Name</FormLabel>
                  <FormControl>
                    <Input placeholder="My Chatbot Config" {...field} />
                  </FormControl>
                  <FormDescription>
                    A name to identify this configuration
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="system_prompt"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>System Prompt</FormLabel>
                  <FormControl>
                    <Textarea 
                      placeholder="You are a helpful assistant..." 
                      className="min-h-[100px]"
                      {...field} 
                    />
                  </FormControl>
                  <FormDescription>
                    This sets the AI's behavior and personality
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="temperature"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Temperature: {field.value.toFixed(1)}</FormLabel>
                  <FormControl>
                    <Slider
                      min={0}
                      max={2}
                      step={0.1}
                      value={[field.value]}
                      onValueChange={(value) => field.onChange(value[0])}
                    />
                  </FormControl>
                  <FormDescription>
                    Lower values make responses more focused, higher values make them more creative
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="model"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>AI Model</FormLabel>
                  <FormControl>
                    <RadioGroup
                      onValueChange={field.onChange}
                      defaultValue={field.value}
                      value={field.value}
                      className="flex flex-col space-y-1"
                    >
                      <FormItem className="flex items-center space-x-3 space-y-0">
                        <FormControl>
                          <RadioGroupItem value="gpt-4o" />
                        </FormControl>
                        <FormLabel className="font-normal">
                          GPT-4o (Most powerful)
                        </FormLabel>
                      </FormItem>
                      <FormItem className="flex items-center space-x-3 space-y-0">
                        <FormControl>
                          <RadioGroupItem value="gpt-4o-mini" />
                        </FormControl>
                        <FormLabel className="font-normal">
                          GPT-4o Mini (Faster)
                        </FormLabel>
                      </FormItem>
                    </RadioGroup>
                  </FormControl>
                  <FormDescription>
                    Select the AI model to use for responses
                  </FormDescription>
                  <FormMessage />
                </FormItem>
              )}
            />

            <DialogFooter>
              <Button type="submit">Save Configuration</Button>
            </DialogFooter>
          </form>
        </Form>
      </DialogContent>
    </Dialog>
  );
}