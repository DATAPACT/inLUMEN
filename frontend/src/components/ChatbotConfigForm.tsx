import React, { useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Form,
  FormControl,
  FormField,
  FormItem,
  FormLabel,
  FormMessage,
} from "@/components/ui/form";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { zodResolver } from "@hookform/resolvers/zod";
import { useForm } from "react-hook-form";
import * as z from "zod";
import {
  ChatbotConfig,
  LLMProvider,
  LLM_PROVIDER_DETAILS,
  createChatbotConfig,
  getDefaultChatbotConfig,
  updateChatbotConfig,
} from "@/services/chatbotService";
import { toast } from "sonner";

const providerValues = ["openrouter", "ollama_cloud", "custom"] as const;

const formSchema = z.object({
  name: z.string().min(1, "Configuration name is required"),
  provider: z.enum(providerValues),
  model: z.string().min(1, "Model name is required"),
  baseUrl: z
    .string()
    .min(1, "Base URL is required")
    .refine((value) => /^https?:\/\/.+/i.test(value), "Use an http(s) OpenAI-compatible base URL"),
  apiKey: z.string().optional(),
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
  onConfigSaved,
}: ChatbotConfigFormProps) {
  const defaultConfig = React.useMemo(() => getDefaultChatbotConfig(), []);

  const form = useForm<z.infer<typeof formSchema>>({
    resolver: zodResolver(formSchema),
    defaultValues: {
      name: initialConfig?.name || "New Configuration",
      provider: initialConfig?.provider || defaultConfig.provider,
      model: initialConfig?.model || defaultConfig.model,
      baseUrl: initialConfig?.baseUrl || defaultConfig.baseUrl,
      apiKey: initialConfig?.apiKey || "",
    },
  });

  const selectedProvider = form.watch("provider");

  useEffect(() => {
    const nextConfig = initialConfig || {
      ...defaultConfig,
      name: "New Configuration",
    };
    form.reset({
      name: nextConfig.name,
      provider: nextConfig.provider,
      model: nextConfig.model,
      baseUrl: nextConfig.baseUrl,
      apiKey: nextConfig.apiKey || "",
    });
  }, [initialConfig, form, defaultConfig]);

  const applyProviderDefaults = (provider: LLMProvider) => {
    const details = LLM_PROVIDER_DETAILS[provider];
    const currentModel = form.getValues("model");
    const defaultModels = Object.values(LLM_PROVIDER_DETAILS).map((item) => item.defaultModel);

    if (details.baseUrl) {
      form.setValue("baseUrl", details.baseUrl, { shouldValidate: true });
    }
    if (!currentModel || defaultModels.includes(currentModel)) {
      form.setValue("model", details.defaultModel, { shouldValidate: true });
    }
  };

  const onSubmit = async (values: z.infer<typeof formSchema>) => {
    try {
      const configData: ChatbotConfig = {
        id: initialConfig?.id,
        name: values.name,
        provider: values.provider,
        model: values.model,
        baseUrl: values.baseUrl,
        apiKey: values.apiKey?.trim() || "",
      };

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
      <DialogContent className="sm:max-w-[540px]">
        <DialogHeader>
          <DialogTitle>
            {initialConfig ? "Edit LLM Configuration" : "New LLM Configuration"}
          </DialogTitle>
          <DialogDescription>
            Configure an OpenAI-compatible endpoint. API keys are kept in this browser only.
          </DialogDescription>
        </DialogHeader>

        <Form {...form}>
          <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-5">
            <FormField
              control={form.control}
              name="name"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Configuration Name</FormLabel>
                  <FormControl>
                    <Input placeholder="OpenRouter GPT-OSS" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="provider"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Provider</FormLabel>
                  <Select
                    onValueChange={(value: LLMProvider) => {
                      field.onChange(value);
                      applyProviderDefaults(value);
                    }}
                    value={field.value}
                  >
                    <FormControl>
                      <SelectTrigger>
                        <SelectValue placeholder="Select an LLM provider" />
                      </SelectTrigger>
                    </FormControl>
                    <SelectContent>
                      {providerValues.map((provider) => (
                        <SelectItem key={provider} value={provider}>
                          {LLM_PROVIDER_DETAILS[provider].label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                  <p className="text-xs text-muted-foreground">
                    {LLM_PROVIDER_DETAILS[selectedProvider].description}
                  </p>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="baseUrl"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>OpenAI-Compatible Base URL</FormLabel>
                  <FormControl>
                    <Input placeholder="https://example.com/v1" {...field} />
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
                  <FormLabel>Model</FormLabel>
                  <FormControl>
                    <Input placeholder="gpt-oss-120b" {...field} />
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="apiKey"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>API Key</FormLabel>
                  <FormControl>
                    <Input type="password" placeholder="Provider API key" autoComplete="off" {...field} />
                  </FormControl>
                  <p className="text-xs text-muted-foreground">
                    Optional. Leave empty to use the backend `LLM_API_KEY` instead.
                  </p>
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
