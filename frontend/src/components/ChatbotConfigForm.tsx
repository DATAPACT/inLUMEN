import React, { useEffect } from 'react';
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
  FormField,
  FormItem,
  FormLabel,
  FormMessage
} from "@/components/ui/form";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import * as z from "zod";
import {
  ChatbotConfig,
  createChatbotConfig,
  updateChatbotConfig
} from "@/services/chatbotService";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { toast } from "sonner";

// ---- Schema ----
const formSchema = z.object({
  name: z.string().min(1, "Configuration name is required"),
  model: z.enum(["gpt-4o", "llama3.1"]),
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

  const form = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      name: initialConfig?.name || "New Configuration",
      model: (initialConfig?.model === "gpt-4o" || initialConfig?.model === "llama3.1")
        ? initialConfig.model
        : "llama3.1",
    },
  });

  useEffect(() => {
    if (initialConfig) {
      form.reset({
        name: initialConfig.name,
        model: (initialConfig.model === "gpt-4o" || initialConfig.model === "llama3.1")
          ? initialConfig.model
          : "llama3.1",
      });
    } else {
      form.reset({
        name: "New Configuration",
        model: "llama3.1",
      });
    }
  }, [initialConfig, form]);

  const onSubmit = async (values: z.infer<typeof formSchema>) => {
    try {
      const configData: ChatbotConfig = {
        name: values.name,
        model: values.model,
      };

      if (initialConfig?.id) {
        configData.id = initialConfig.id;
      }

      const savedConfig = initialConfig?.id
        ? await updateChatbotConfig(configData)
        : await createChatbotConfig(configData);

      if (!savedConfig) throw new Error("Failed to save configuration");

      onConfigSaved(savedConfig);
      onClose();
    } catch (error) {
      console.error("Error saving configuration:", error);
      toast.error("Failed to save configuration", {
        description: error instanceof Error ? error.message : "Unknown error occurred",
      });
    }
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="sm:max-w-[500px]">
        <DialogHeader>
          <DialogTitle>
            {initialConfig ? "Edit Configuration" : "New Configuration"}
          </DialogTitle>
          <DialogDescription>
            Configure the AI backend for your pipeline assistant
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
                      value={field.value}
                      className="flex flex-col space-y-1"
                    >
                      <FormItem className="flex items-center space-x-3">
                        <FormControl>
                          <RadioGroupItem value="llama3.1" />
                        </FormControl>
                        <FormLabel className="font-normal">
                          Llama 3.1 (Local)
                        </FormLabel>
                      </FormItem>

                      <FormItem className="flex items-center space-x-3">
                        <FormControl>
                          <RadioGroupItem value="gpt-4o" />
                        </FormControl>
                        <FormLabel className="font-normal">
                          GPT-4o (Backend Key)
                        </FormLabel>
                      </FormItem>
                    </RadioGroup>
                  </FormControl>
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
