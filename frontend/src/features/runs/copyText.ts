import { toast } from 'sonner';

/** Copy text to the clipboard and say whether it worked. */
export const copyText = async (text: string, what: string) => {
  try {
    await navigator.clipboard.writeText(text);
    toast.success(`Copied ${what}`);
  } catch {
    toast.error(`Couldn't copy the ${what}.`);
  }
};
