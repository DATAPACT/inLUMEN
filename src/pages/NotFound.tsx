import { useLocation } from "react-router-dom";
import { useEffect } from "react";
import { Button } from "@/components/ui/button";
import { ArrowLeft } from "lucide-react";

const NotFound = () => {
  const location = useLocation();

  useEffect(() => {
    console.error(
      "404 Error: User attempted to access non-existent route:",
      location.pathname
    );
  }, [location.pathname]);

  return (
    <div className="min-h-screen flex items-center justify-center bg-canvas-DEFAULT">
      <div className="text-center space-y-6 max-w-md px-6 animate-scale-in">
        <div className="text-6xl font-bold bg-gradient-to-r from-primary/90 to-accent/90 bg-clip-text text-transparent">404</div>
        <h1 className="text-2xl font-medium">Page not found</h1>
        <p className="text-muted-foreground">The page you're looking for doesn't exist or has been moved.</p>
        <Button asChild>
          <a href="/" className="inline-flex items-center gap-2">
            <ArrowLeft className="w-4 h-4" />
            Return to Flow Builder
          </a>
        </Button>
      </div>
    </div>
  );
};

export default NotFound;