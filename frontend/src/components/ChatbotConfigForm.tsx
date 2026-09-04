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
import { OpenRouterModelCombobox } from "@/components/OpenRouterModelCombobox";
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
import {
  fetchOpenRouterModels,
  type OpenRouterModel,
} from "@/services/openRouterModels";
import { toast } from "sonner";

const providerValues = ["openrouter", "ollama_cloud", "custom"] as const;

const formSchema = z.object({
  name: z.string().min(1, "Configuration name is required"),
  provider: z.enum(providerValues),
  model: z.string().min(1, "Model name is required"),
  codegenModel: z.string().min(1, "Code generation model is required"),
  openrouterProviderOnly: z.array(z.string()),
  codegenOpenrouterProviderOnly: z.array(z.string()),
  baseUrl: z
    .string()
    .min(1, "Base URL is required")
    .refine((value) => /^https?:\/\/.+/i.test(value), "Use an http(s) OpenAI-compatible base URL"),
  apiKey: z.string().trim(),
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
      codegenModel: initialConfig?.codegenModel || defaultConfig.model,
      openrouterProviderOnly: initialConfig?.openrouterProviderOnly || [],
      codegenOpenrouterProviderOnly: initialConfig?.codegenOpenrouterProviderOnly || [],
      baseUrl: initialConfig?.baseUrl || defaultConfig.baseUrl,
      apiKey: initialConfig?.apiKey || "",
    },
  });

  const selectedProvider = form.watch("provider");
  const [openRouterModels, setOpenRouterModels] = React.useState<OpenRouterModel[]>([]);
  const [isLoadingModels, setIsLoadingModels] = React.useState(false);
  const [modelCatalogError, setModelCatalogError] = React.useState("");

  useEffect(() => {
    const nextConfig = initialConfig || {
      ...defaultConfig,
      name: "New Configuration",
    };
    form.reset({
      name: nextConfig.name,
      provider: nextConfig.provider,
      model: nextConfig.model,
      codegenModel: nextConfig.codegenModel || nextConfig.model,
      openrouterProviderOnly: nextConfig.openrouterProviderOnly || [],
      codegenOpenrouterProviderOnly: nextConfig.codegenOpenrouterProviderOnly || [],
      baseUrl: nextConfig.baseUrl,
      apiKey: nextConfig.apiKey || "",
    });
  }, [initialConfig, form, defaultConfig]);

  useEffect(() => {
    if (!isOpen || selectedProvider !== "openrouter") return;
    const controller = new AbortController();
    setIsLoadingModels(true);
    setModelCatalogError("");
    fetchOpenRouterModels(controller.signal)
      .then((models) => {
        setOpenRouterModels(models);
        const routes = [
          ["model", "openrouterProviderOnly"],
          ["codegenModel", "codegenOpenrouterProviderOnly"],
        ] as const;
        routes.forEach(([modelField, routeField]) => {
          const modelId = form.getValues(modelField).trim().toLowerCase();
          const match = models.find((model) => model.id.toLowerCase() === modelId);
          if (match) {
            form.setValue(routeField, match.cerebrasAvailable ? ["cerebras"] : []);
          }
        });
      })
      .catch((error) => {
        if (error instanceof DOMException && error.name === "AbortError") return;
        setModelCatalogError(error instanceof Error ? error.message : "Could not load models");
      })
      .finally(() => {
        if (!controller.signal.aborted) setIsLoadingModels(false);
      });
    return () => controller.abort();
  }, [isOpen, selectedProvider, form]);

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
    if (provider !== "openrouter") {
      form.setValue("openrouterProviderOnly", []);
      form.setValue("codegenOpenrouterProviderOnly", []);
    }
  };

  const onSubmit = async (values: z.infer<typeof formSchema>) => {
    try {
      if (!values.apiKey.trim() && !initialConfig?.hasApiKey) {
        form.setError("apiKey", { message: "API key is required for LLM calls" });
        return;
      }
      const configData: ChatbotConfig = {
        id: initialConfig?.id,
        name: values.name,
        provider: values.provider,
        model: values.model,
        codegenModel: values.codegenModel.trim(),
        openrouterProviderOnly:
          values.provider === "openrouter" ? values.openrouterProviderOnly : [],
        codegenOpenrouterProviderOnly:
          values.provider === "openrouter" ? values.codegenOpenrouterProviderOnly : [],
        baseUrl: values.baseUrl,
        apiKey: values.apiKey?.trim() || "",
        hasApiKey: initialConfig?.hasApiKey,
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
      <DialogContent className="max-h-[90vh] overflow-y-auto sm:max-w-[680px]">
        <DialogHeader>
          <DialogTitle>
            {initialConfig ? "Edit LLM Configuration" : "New LLM Configuration"}
          </DialogTitle>
          <DialogDescription>
            Configure an OpenAI-compatible endpoint. Saved credentials are encrypted by the gateway and are never returned to the browser.
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
                    {selectedProvider === "openrouter" ? (
                      <OpenRouterModelCombobox
                        value={field.value}
                        models={openRouterModels}
                        isLoading={isLoadingModels}
                        catalogError={modelCatalogError}
                        placeholder="Search OpenRouter models"
                        onChange={(modelId, model) => {
                          field.onChange(modelId);
                          form.setValue(
                            "openrouterProviderOnly",
                            model?.cerebrasAvailable ? ["cerebras"] : [],
                            { shouldDirty: true },
                          );
                        }}
                      />
                    ) : (
                      <Input placeholder="gpt-oss-120b" {...field} />
                    )}
                  </FormControl>
                  <FormMessage />
                </FormItem>
              )}
            />

            <FormField
              control={form.control}
              name="codegenModel"
              render={({ field }) => (
                <FormItem>
                  <FormLabel>Code Generation Model</FormLabel>
                  <FormControl>
                    {selectedProvider === "openrouter" ? (
                      <OpenRouterModelCombobox
                        value={field.value}
                        models={openRouterModels}
                        isLoading={isLoadingModels}
                        catalogError={modelCatalogError}
                        placeholder="Search code-capable models"
                        onChange={(modelId, model) => {
                          field.onChange(modelId);
                          form.setValue(
                            "codegenOpenrouterProviderOnly",
                            model?.cerebrasAvailable ? ["cerebras"] : [],
                            { shouldDirty: true },
                          );
                        }}
                      />
                    ) : (
                      <Input placeholder="openai/gpt-5.2-codex" {...field} />
                    )}
                  </FormControl>
                  <p className="text-xs text-muted-foreground">
                    Used for node and pipeline code generation. It shares this
                    configuration&apos;s provider, base URL, and API key.
                  </p>
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
                    <Input type="password" placeholder={initialConfig?.hasApiKey ? "Saved securely — enter a replacement to rotate" : "Provider API key"} autoComplete="new-password" {...field} />
                  </FormControl>
                  <p className="text-xs text-muted-foreground">
                    {initialConfig?.hasApiKey
                      ? "A credential is saved securely. Leave this blank to keep it, or enter a new value to replace it."
                      : "Required for LLM calls. It is encrypted by the gateway and is never returned to this browser."}
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
