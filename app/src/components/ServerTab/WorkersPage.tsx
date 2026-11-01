import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { formatDistanceToNow } from 'date-fns';
import { AlertCircle, CheckCircle2, Cpu, Loader2, RefreshCw, ShieldCheck, Trash2, WifiOff } from 'lucide-react';
import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent, AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle } from '@/components/ui/alert-dialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { apiClient } from '@/lib/api/client';
import type { RemoteVoiceWorker } from '@/lib/api/types';
import { useToast } from '@/components/ui/use-toast';

const WORKERS_QUERY_KEY = ['remote-voice-workers'];

function lastSeenLabel(value: string | null, neverLabel: string): string {
  if (!value) return neverLabel;
  const iso = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(value) ? value : `${value}Z`;
  const timestamp = new Date(iso);
  if (Number.isNaN(timestamp.getTime())) return neverLabel;
  return `${formatDistanceToNow(timestamp, { addSuffix: true })} · ${timestamp.toLocaleString()}`;
}

function modelLabel(model: { engine: string; model_size?: string | null }): string {
  const engines: Record<string, string> = {
    qwen: 'Qwen TTS',
    qwen_custom_voice: 'Qwen Custom Voice',
    qwen_voice_design: 'Qwen Voice Design',
    luxtts: 'LuxTTS',
    chatterbox: 'Chatterbox',
    chatterbox_turbo: 'Chatterbox Turbo',
    tada: 'TADA',
    kokoro: 'Kokoro',
    omnivoice: 'OmniVoice',
  };
  const name = engines[model.engine] ?? model.engine;
  return model.model_size ? `${name} ${model.model_size}` : name;
}

function WorkerCard({ worker, onRevoke }: { worker: RemoteVoiceWorker; onRevoke: () => void }) {
  const { t } = useTranslation();
  const models = worker.capabilities.models ?? [];

  return (
    <article className="rounded-xl border border-border/70 bg-card p-5 shadow-sm">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <div className="mt-0.5 flex h-10 w-10 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
            <Cpu className="h-5 w-5" aria-hidden="true" />
          </div>
          <div className="min-w-0">
            <h2 className="truncate text-base font-semibold">{worker.name}</h2>
            <p className="mt-1 text-sm text-muted-foreground">
              {[worker.capabilities.platform, worker.capabilities.gpu].filter(Boolean).join(' · ') || t('settings.workers.unknownDevice')}
            </p>
          </div>
        </div>
        <Badge
          variant="outline"
          className={worker.online
            ? 'gap-1.5 border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300'
            : 'gap-1.5 border-muted-foreground/25 text-muted-foreground'}
        >
          {worker.online
            ? <CheckCircle2 className="h-3.5 w-3.5" aria-hidden="true" />
            : <WifiOff className="h-3.5 w-3.5" aria-hidden="true" />}
          {worker.online ? t('settings.workers.online') : t('settings.workers.offline')}
        </Badge>
      </div>

      <div className="mt-5 grid gap-4 border-t border-border/60 pt-4 sm:grid-cols-[1fr_auto]">
        <div className="space-y-3">
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('settings.workers.models')}</p>
            {models.length ? (
              <div className="mt-2 flex flex-wrap gap-2">
                {models.map((model, index) => (
                  <Badge key={`${model.engine}-${model.model_size ?? 'default'}-${index}`} variant="secondary">
                    {modelLabel(model)}
                  </Badge>
                ))}
              </div>
            ) : (
              <p className="mt-1 text-sm text-muted-foreground">{t('settings.workers.noModels')}</p>
            )}
          </div>
          <div>
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{t('settings.workers.lastHeartbeat')}</p>
            <p className="mt-1 text-sm" title={worker.lastSeenAt ?? undefined}>
              {lastSeenLabel(worker.lastSeenAt, t('settings.workers.never'))}
            </p>
          </div>
        </div>
        {worker.enabled && (
          <Button variant="outline" size="sm" className="self-end text-destructive hover:text-destructive" onClick={onRevoke}>
            <Trash2 aria-hidden="true" />
            {t('settings.workers.revoke')}
          </Button>
        )}
      </div>
    </article>
  );
}

export function WorkersPage() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [selectedWorker, setSelectedWorker] = useState<RemoteVoiceWorker | null>(null);
  const workersQuery = useQuery({
    queryKey: WORKERS_QUERY_KEY,
    queryFn: () => apiClient.listRemoteWorkers(),
    refetchInterval: 10_000,
    refetchOnWindowFocus: true,
  });
  const revokeMutation = useMutation({
    mutationFn: (workerId: string) => apiClient.revokeRemoteWorker(workerId),
    onSuccess: () => {
      setSelectedWorker(null);
      void queryClient.invalidateQueries({ queryKey: WORKERS_QUERY_KEY });
      toast({ title: t('settings.workers.revokedTitle'), description: t('settings.workers.revokedDescription') });
    },
    onError: (error) => {
      toast({ title: t('settings.workers.revokeFailed'), description: error instanceof Error ? error.message : String(error), variant: 'destructive' });
    },
  });

  const workers = workersQuery.data ?? [];
  const onlineCount = workers.filter((worker) => worker.online && worker.enabled).length;

  return (
    <div className="mx-auto max-w-4xl space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">{t('settings.workers.title')}</h1>
          <p className="mt-1 max-w-2xl text-sm text-muted-foreground">{t('settings.workers.description')}</p>
        </div>
        <Button variant="outline" onClick={() => void workersQuery.refetch()} disabled={workersQuery.isFetching}>
          {workersQuery.isFetching ? <Loader2 className="animate-spin" aria-hidden="true" /> : <RefreshCw aria-hidden="true" />}
          {t('settings.workers.refresh')}
        </Button>
      </header>

      <div className="flex flex-wrap items-center gap-3 rounded-xl border border-border/60 bg-muted/20 p-4">
        <ShieldCheck className="h-5 w-5 text-accent" aria-hidden="true" />
        <p className="text-sm text-muted-foreground">{t('settings.workers.dispatchInfo')}</p>
        <Badge variant="secondary" className="ml-auto">{t('settings.workers.onlineCount', { count: onlineCount })}</Badge>
      </div>

      {workersQuery.isPending ? (
        <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground"><Loader2 className="h-4 w-4 animate-spin" />{t('settings.workers.loading')}</div>
      ) : workersQuery.error ? (
        <div role="alert" className="flex items-start gap-3 rounded-xl border border-destructive/30 bg-destructive/5 p-4 text-sm">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0 text-destructive" aria-hidden="true" />
          <div><p className="font-medium">{t('settings.workers.loadFailed')}</p><p className="mt-1 text-muted-foreground">{workersQuery.error instanceof Error ? workersQuery.error.message : String(workersQuery.error)}</p></div>
        </div>
      ) : workers.length === 0 ? (
        <div className="rounded-xl border border-dashed border-border p-8 text-center">
          <Cpu className="mx-auto h-8 w-8 text-muted-foreground" aria-hidden="true" />
          <h2 className="mt-3 font-medium">{t('settings.workers.emptyTitle')}</h2>
          <p className="mx-auto mt-1 max-w-lg text-sm text-muted-foreground">{t('settings.workers.emptyDescription')}</p>
        </div>
      ) : (
        <div className="space-y-3">
          {workers.map((worker) => (
            <WorkerCard key={worker.id} worker={worker} onRevoke={() => setSelectedWorker(worker)} />
          ))}
        </div>
      )}

      <AlertDialog open={!!selectedWorker} onOpenChange={(open) => { if (!open) setSelectedWorker(null); }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{t('settings.workers.revokeTitle', { name: selectedWorker?.name ?? '' })}</AlertDialogTitle>
            <AlertDialogDescription>{t('settings.workers.revokeDescriptionLong')}</AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <AlertDialogCancel>{t('common.cancel')}</AlertDialogCancel>
            <AlertDialogAction
              disabled={revokeMutation.isPending}
              className="bg-destructive text-destructive-foreground hover:bg-destructive/90"
              onClick={() => { if (selectedWorker) revokeMutation.mutate(selectedWorker.id); }}
            >
              {revokeMutation.isPending ? <Loader2 className="animate-spin" aria-hidden="true" /> : <Trash2 aria-hidden="true" />}
              {t('settings.workers.revoke')}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  );
}
