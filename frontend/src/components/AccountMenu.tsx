import { ChevronDown, LogOut, UserRound } from 'lucide-react';
import { useAuthSession } from '@/context/authSession';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

export function AccountMenu() {
  const { session, authEnabled, embedded, signOut } = useAuthSession();
  const user = session?.user;
  const name = user?.display_name || 'Signed-in user';
  const workspace = session?.workspaces.find(item => item.id === session.active_workspace_id);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="sm" className="h-8 gap-2 text-xs" aria-label={`Account: ${name}`} title={name}>
          <UserRound className="h-4 w-4 shrink-0" />
          <span className="hidden max-w-32 truncate md:inline">{name}</span>
          <ChevronDown className="h-3 w-3 shrink-0" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72 max-w-[calc(100vw-2rem)]">
        <DropdownMenuLabel className="break-words">{name}</DropdownMenuLabel>
        <div className="space-y-3 px-2 pb-3 text-xs">
          <div>
            <p className="text-muted-foreground">Current workspace</p>
            <p className="break-words font-medium">{workspace?.name || 'Workspace'}</p>
            <p className="mt-1 break-all font-mono text-[10px] text-muted-foreground">{session?.active_workspace_id}</p>
          </div>
          <div>
            <p className="text-muted-foreground">User ID</p>
            <p className="break-all font-mono text-[10px]">{user?.id}</p>
          </div>
          {!authEnabled && <p className="text-muted-foreground">Local mode · Authentication disabled</p>}
          {authEnabled && embedded && <p className="text-muted-foreground">Sign out through the host application.</p>}
        </div>
        {authEnabled && !embedded && <>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => { void signOut(); }}>
            <LogOut className="mr-2 h-4 w-4" />Sign out
          </DropdownMenuItem>
        </>}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
