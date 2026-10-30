import { Film, Pause, Play, Scissors, SkipBack, Youtube } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Slider } from '@/components/ui/slider';

const MAX_CLIP_SECONDS = 29;
const MIN_CLIP_SECONDS = 0.1;

type Player = {
  destroy: () => void;
  getCurrentTime: () => number;
  getDuration: () => number;
  loadVideoById: (video: string | { videoId: string; startSeconds: number; endSeconds: number }) => void;
  pauseVideo: () => void;
  playVideo: () => void;
  seekTo: (seconds: number, allowSeekAhead: boolean) => void;
};

type PlayerEvents = {
  onReady: (event: { target: Player }) => void;
  onStateChange?: (event: { data: number }) => void;
};

type PlayerConstructor = new (element: HTMLElement, options: {
  videoId: string;
  playerVars?: Record<string, string | number>;
  events?: PlayerEvents;
}) => Player;

type YouTubeWindow = Window & {
  YT?: { Player: PlayerConstructor };
  onYouTubeIframeAPIReady?: () => void;
};

let youtubeApiPromise: Promise<{ Player: PlayerConstructor }> | null = null;

function loadYouTubeApi(): Promise<{ Player: PlayerConstructor }> {
  const win = window as YouTubeWindow;
  if (win.YT?.Player) return Promise.resolve(win.YT);
  if (youtubeApiPromise) return youtubeApiPromise;

  youtubeApiPromise = new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => reject(new Error('YouTube player did not load.')), 15000);
    const previousCallback = win.onYouTubeIframeAPIReady;
    win.onYouTubeIframeAPIReady = () => {
      previousCallback?.();
      window.clearTimeout(timeout);
      if (win.YT?.Player) resolve(win.YT);
      else reject(new Error('YouTube player is unavailable.'));
    };
    if (!document.querySelector('script[data-voicebox-youtube-api]')) {
      const script = document.createElement('script');
      script.src = 'https://www.youtube.com/iframe_api';
      script.async = true;
      script.dataset.voiceboxYoutubeApi = 'true';
      script.onerror = () => {
        window.clearTimeout(timeout);
        youtubeApiPromise = null;
        reject(new Error('Could not load the YouTube player API.'));
      };
      document.head.appendChild(script);
    }
  });
  return youtubeApiPromise;
}

function getVideoId(value: string): string | null {
  try {
    const url = new URL(value.trim());
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
    const host = url.hostname.toLowerCase().replace(/^www\./, '');
    if (host === 'youtu.be') return /^[\w-]{11}$/.test(url.pathname.slice(1)) ? url.pathname.slice(1) : null;
    if (!['youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtube-nocookie.com'].includes(host)) return null;
    const path = url.pathname.split('/').filter(Boolean);
    const candidate = url.searchParams.get('v') || (['shorts', 'embed', 'live'].includes(path[0]) ? path[1] : null);
    return candidate && /^[\w-]{11}$/.test(candidate) ? candidate : null;
  } catch {
    return null;
  }
}

function timeLabel(seconds: number): string {
  const value = Math.max(0, Math.floor(seconds));
  return `${Math.floor(value / 60)}:${String(value % 60).padStart(2, '0')}`;
}

interface YouTubeSampleSourceProps {
  onExtracted: (file: File) => void;
  onExtractYouTube: (videoUrl: string, startSeconds: number, endSeconds: number) => Promise<Blob>;
  onExtract: (file: File, startSeconds: number, endSeconds: number) => Promise<Blob>;
}

export function YouTubeSampleSource({ onExtracted, onExtractYouTube, onExtract }: YouTubeSampleSourceProps) {
  const [url, setUrl] = useState('');
  const videoId = useMemo(() => getVideoId(url), [url]);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [startSeconds, setStartSeconds] = useState(0);
  const [endSeconds, setEndSeconds] = useState(0);
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [permissionConfirmed, setPermissionConfirmed] = useState(false);
  const [isPlaying, setIsPlaying] = useState(false);
  const [isExtracting, setIsExtracting] = useState(false);
  const [error, setError] = useState('');
  const playerElement = useRef<HTMLDivElement>(null);
  const player = useRef<Player | null>(null);
  const durationRef = useRef(0);

  useEffect(() => {
    durationRef.current = duration;
  }, [duration]);

  useEffect(() => {
    setDuration(0);
    setCurrentTime(0);
    setStartSeconds(0);
    setEndSeconds(0);
    setSourceFile(null);
    setIsPlaying(false);
    setError('');
  }, [videoId]);

  useEffect(() => {
    if (!videoId || !playerElement.current) return;
    let cancelled = false;
    loadYouTubeApi().then((YT) => {
      if (cancelled || !playerElement.current) return;
      player.current?.destroy();
      player.current = new YT.Player(playerElement.current, {
        videoId,
        playerVars: { enablejsapi: 1, origin: window.location.origin, playsinline: 1, rel: 0 },
        events: {
          onReady: ({ target }) => {
            player.current = target;
            const length = target.getDuration();
            if (length > 0) {
              durationRef.current = length;
              setDuration(length);
              setEndSeconds(Math.min(length, MAX_CLIP_SECONDS));
            }
          },
          onStateChange: ({ data }) => setIsPlaying(data === 1),
        },
      });
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : 'Could not load the video.'));
    return () => {
      cancelled = true;
      player.current?.destroy();
      player.current = null;
    };
  }, [videoId]);

  useEffect(() => {
    if (!videoId) return;
    const timer = window.setInterval(() => {
      if (!player.current) return;
      try {
        setCurrentTime(player.current.getCurrentTime());
        const length = player.current.getDuration();
        if (length > 0 && length !== durationRef.current) {
          durationRef.current = length;
          setDuration(length);
          setEndSeconds((value) => value > 0 ? Math.min(value, length, MAX_CLIP_SECONDS) : Math.min(length, MAX_CLIP_SECONDS));
        }
      } catch { /* Player has not finished initializing yet. */ }
    }, 250);
    return () => window.clearInterval(timer);
  }, [videoId]);

  function setSelection(nextStart: number, nextEnd: number) {
    const safeDuration = Math.max(0, duration);
    let start = Math.max(0, Math.min(nextStart, safeDuration));
    let end = Math.max(0, Math.min(nextEnd, safeDuration));
    if (end - start > MAX_CLIP_SECONDS) {
      if (start !== startSeconds) end = start + MAX_CLIP_SECONDS;
      else start = end - MAX_CLIP_SECONDS;
    }
    if (end - start < MIN_CLIP_SECONDS) {
      if (end !== endSeconds) end = Math.min(safeDuration, start + MIN_CLIP_SECONDS);
      else start = Math.max(0, end - MIN_CLIP_SECONDS);
    }
    setStartSeconds(start);
    setEndSeconds(end);
  }

  function markStart() {
    setSelection(Math.min(currentTime, Math.max(0, duration - MIN_CLIP_SECONDS)), endSeconds);
  }

  function markEnd() {
    setSelection(startSeconds, currentTime);
  }

  function previewSelectedClip() {
    if (!player.current || !videoId || selectedDuration < MIN_CLIP_SECONDS) return;
    player.current.loadVideoById({ videoId, startSeconds, endSeconds });
    setIsPlaying(true);
  }

  async function extractSelectedClip() {
    if (!sourceFile || !permissionConfirmed || endSeconds <= startSeconds) return;
    setError('');
    setIsExtracting(true);
    try {
      const blob = await onExtract(sourceFile, startSeconds, endSeconds);
      onExtracted(new File([blob], 'youtube-voice-sample.wav', { type: 'audio/wav' }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not extract audio from this file.');
    } finally {
      setIsExtracting(false);
    }
  }

  async function extractSelectedYouTubeClip() {
    if (!videoId || !permissionConfirmed || selectedDuration < MIN_CLIP_SECONDS || selectedDuration > MAX_CLIP_SECONDS) return;
    setError('');
    setIsExtracting(true);
    try {
      const blob = await onExtractYouTube(url, startSeconds, endSeconds);
      onExtracted(new File([blob], 'youtube-voice-sample.wav', { type: 'audio/wav' }));
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : 'Could not retrieve audio for this video.');
    } finally {
      setIsExtracting(false);
    }
  }

  const selectedDuration = Math.max(0, endSeconds - startSeconds);

  return (
    <div className="space-y-5 rounded-xl border bg-card p-4 sm:p-6">
      <div className="space-y-1">
        <h3 className="flex items-center gap-2 text-base font-semibold"><Youtube className="h-5 w-5 text-red-600" /> YouTube sample editor</h3>
        <p className="text-sm text-muted-foreground">Preview a video, scrub to the right moment, then mark a clip up to 29 seconds long.</p>
      </div>

      <div className="flex flex-col gap-2 sm:flex-row">
        <Input
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="Paste a YouTube video link"
          aria-label="YouTube video URL"
        />
        <Button type="button" variant="outline" disabled={!videoId} onClick={() => player.current?.playVideo()}>
          <Play /> Play video
        </Button>
      </div>
      {url && !videoId && <p className="text-sm text-destructive">Enter a valid YouTube video link.</p>}

      {videoId && (
        <>
          <div className="mx-auto w-full max-w-5xl overflow-hidden rounded-xl border bg-black shadow-sm">
            <div className="aspect-video w-full">
              <div key={videoId} ref={playerElement} className="h-full w-full" />
            </div>
          </div>

          {duration > 0 ? (
            <section aria-label="Clip timeline" className="space-y-4 rounded-xl bg-muted/40 p-4 sm:p-5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <p className="text-sm font-semibold">Choose a section</p>
                  <p className="text-xs text-muted-foreground">Drag both handles or scrub the playhead, then set the in and out points.</p>
                </div>
                <span className="rounded-full border bg-background px-3 py-1 text-sm font-semibold tabular-nums">
                  {timeLabel(startSeconds)} – {timeLabel(endSeconds)} <span className="text-muted-foreground">({selectedDuration.toFixed(1)}s / 29s max)</span>
                </span>
              </div>

              <div className="space-y-2">
                <div className="flex justify-between text-xs tabular-nums text-muted-foreground"><span>{timeLabel(0)}</span><span>{timeLabel(duration)}</span></div>
                <Slider
                  min={0}
                  max={Math.max(0.1, duration)}
                  step={0.1}
                  value={[startSeconds, endSeconds]}
                  minStepsBetweenThumbs={1}
                  onValueChange={([start, end]) => setSelection(start, end)}
                  aria-label="Selected clip start and end"
                  className="py-3"
                />
                <p className="text-center text-xs text-muted-foreground">Selected audio section</p>
              </div>

              <div className="space-y-2 rounded-lg border bg-background p-3">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm font-medium tabular-nums">Playhead {timeLabel(currentTime)} <span className="text-muted-foreground">/ {timeLabel(duration)}</span></span>
                  <div className="flex gap-2">
                    <Button type="button" size="sm" variant="outline" aria-label="Preview selected section" onClick={previewSelectedClip}>
                      <SkipBack /> <span className="hidden sm:inline">Preview selection</span>
                    </Button>
                    <Button type="button" size="sm" variant="outline" aria-label={isPlaying ? 'Pause preview' : 'Play preview'} onClick={() => {
                      if (isPlaying) { player.current?.pauseVideo(); setIsPlaying(false); }
                      else { player.current?.playVideo(); setIsPlaying(true); }
                    }}>
                      {isPlaying ? <Pause /> : <Play />}
                    </Button>
                  </div>
                </div>
                <Slider
                  min={0}
                  max={Math.max(0.1, duration)}
                  step={0.1}
                  value={[Math.min(currentTime, duration)]}
                  onValueChange={([value]) => setCurrentTime(value)}
                  onValueCommit={([value]) => player.current?.seekTo(value, true)}
                  aria-label="Scrub video playhead"
                />
              </div>

              <div className="flex flex-wrap gap-2">
                <Button type="button" size="sm" variant="outline" onClick={markStart}>Set start at playhead</Button>
                <Button type="button" size="sm" variant="outline" onClick={markEnd}>Set end at playhead</Button>
              </div>
            </section>
          ) : <p className="text-sm text-muted-foreground">Loading video details…</p>}
        </>
      )}

      {videoId && duration > 0 && (
        <div className="space-y-3 rounded-xl border p-4">
        <label className="flex cursor-pointer items-start gap-2 text-sm">
          <input type="checkbox" checked={permissionConfirmed} onChange={(event) => setPermissionConfirmed(event.target.checked)} className="mt-1" />
          <span>I own or am authorised to download and use this media, and I have permission to create this voice profile from the speaker’s voice.</span>
        </label>
        <p className="text-xs text-muted-foreground">Voicebox processes only the selected section, stores temporary media on its data disk, and removes the working files when extraction finishes. Private or restricted videos may not be available.</p>
        <Button type="button" onClick={extractSelectedYouTubeClip} disabled={!permissionConfirmed || selectedDuration < MIN_CLIP_SECONDS || selectedDuration > MAX_CLIP_SECONDS || isExtracting}>
          <Scissors className="mr-2 h-4 w-4" />{isExtracting ? 'Extracting selected audio…' : `Extract ${selectedDuration.toFixed(1)}s from YouTube`}
        </Button>
        <details className="rounded-lg bg-muted/40 p-3">
          <summary className="cursor-pointer text-sm font-medium">Use an authorised source file instead</summary>
          <div className="mt-3 space-y-3">
            <Label htmlFor="voice-source-media" className="flex items-center gap-2 text-sm"><Film className="h-4 w-4" /> Audio or video file</Label>
            <Input
              key={videoId ?? 'no-video'}
              id="voice-source-media"
              type="file"
              accept="audio/*,video/*,.mkv,.mov,.m4a,.flac,.ogg,.opus"
              onChange={(event) => {
                const file = event.target.files?.[0] ?? null;
                if (file && file.size > 250 * 1024 * 1024) {
                  setSourceFile(null);
                  setError('Source file must be 250 MB or smaller.');
                  return;
                }
                setSourceFile(file);
                setError('');
              }}
            />
            <Button type="button" variant="outline" onClick={extractSelectedClip} disabled={!sourceFile || !permissionConfirmed || selectedDuration < MIN_CLIP_SECONDS || selectedDuration > MAX_CLIP_SECONDS || isExtracting}>
              <Film className="mr-2 h-4 w-4" />{isExtracting ? 'Extracting clip…' : 'Extract from selected file'}
            </Button>
          </div>
        </details>
        </div>
      )}
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
