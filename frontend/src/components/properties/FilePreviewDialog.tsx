import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { Edit, Save } from 'lucide-react';

export type PreviewType = 'text' | 'image' | 'binary';

type FilePreviewDialogProps = {
  file: File | null;
  previewContent: string;
  previewType: PreviewType;
  isEditing: boolean;
  editedContent: string;
  onClose: () => void;
  onStartEditing: () => void;
  onCancelEditing: () => void;
  onEditedContentChange: (content: string) => void;
  onSaveChanges: () => void;
};

export const FilePreviewDialog = ({
  file,
  previewContent,
  previewType,
  isEditing,
  editedContent,
  onClose,
  onStartEditing,
  onCancelEditing,
  onEditedContentChange,
  onSaveChanges,
}: FilePreviewDialogProps) => (
  <Dialog open={!!file} onOpenChange={onClose}>
    <DialogContent className="max-w-4xl max-h-[80vh] overflow-hidden">
      <DialogHeader>
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <DialogTitle>{file?.name}</DialogTitle>
            <Badge variant="outline" className="text-xs">
              {previewType === 'image' ? 'Image' : previewType === 'text' ? 'Text' : 'Binary'}
            </Badge>
          </div>
          <div className="flex items-center gap-2">
            {previewType === 'text' && !isEditing && (
              <Button
                variant="outline"
                size="sm"
                onClick={onStartEditing}
              >
                <Edit className="w-4 h-4 mr-1" />
                Edit
              </Button>
            )}
            {isEditing && (
              <>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={onCancelEditing}
                >
                  Cancel
                </Button>
                <Button
                  variant="default"
                  size="sm"
                  onClick={onSaveChanges}
                >
                  <Save className="w-4 h-4 mr-1" />
                  Save Changes
                </Button>
              </>
            )}
          </div>
        </div>
      </DialogHeader>

      <div className="flex-1 overflow-auto">
        {previewType === 'image' ? (
          <div className="flex justify-center p-4">
            <img
              src={previewContent}
              alt={file?.name}
              className="max-w-full max-h-[60vh] object-contain rounded-lg border"
            />
          </div>
        ) : isEditing && previewType === 'text' ? (
          <Textarea
            value={editedContent}
            onChange={(e) => onEditedContentChange(e.target.value)}
            className="min-h-[400px] font-mono text-sm resize-none"
            placeholder="Edit your file content here..."
          />
        ) : (
          <pre className="whitespace-pre-wrap text-sm font-mono bg-muted/50 p-4 rounded">
            {previewContent}
          </pre>
        )}
      </div>
    </DialogContent>
  </Dialog>
);
