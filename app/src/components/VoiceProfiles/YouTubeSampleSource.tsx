import { Film, Scissors, Youtube } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Slider } from '@/components/ui/slider';

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
  onExtract: (file: File, startSeconds: number, endSeconds: number) => Promise<Blob>;
}

export function YouTubeSampleSource({ onExtracted, onExtract }: YouTubeSampleSourceProps) {
  const [url, setUrl] = useState('');
  const videoId = useMemo(() => getVideoId(url), [url]);
  const [duration, setDuration] = useState(0);
  const [currentTime, setCurrentTime] = useState(0);
  const [startSeconds, setStartSeconds] = useState(0);
  const [endSeconds, setEndSeconds] = useState(0);
  const [sourceFile, setSourceFile] = useState<File | null>(null);
  const [permissionConfirmed, setPermissionConfirmed] = useState(false);
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
    setError('');
  }, [videoId]);

  // Each URL gets its own embed player; the official player remains in control of playback.
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
            durationRef.current = length;
            setDuration(length);
            setEndSeconds(Math.min(length, 15));
          },
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
          setEndSeconds((value) => value > 0 ? Math.min(value, length) : Math.min(15, length));
        }
      } catch { /* Player has not finished initializing yet. */ }
    }, 250);
    return () => window.clearInterval(timer);
  }, [videoId]);

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

  return (
    <div className="space-y-4 rounded-lg border p-4">
      <div className="space-y-1">
        <h3 className="flex items-center gap-2 text-sm font-semibold"><Youtube className="h-4 w-4" /> YouTube clip reference</h3>
        <p className="text-sm text-muted-foreground">
          Preview the video and mark a short segment. YouTube’s embedded player does not provide an audio-download API, so Voicebox will trim a source file you upload; it will not download media from the link.
        </p>
      </div>

      <div className="flex gap-2">
        <Input
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://www.youtube.com/watch?v=…"
          aria-label="YouTube video URL"
        />
        <Button type="button" variant="outline" disabled={!videoId} onClick={() => player.current?.playVideo()}>
          Preview
        </Button>
      </div>
      {url && !videoId && <p className="text-sm text-destructive">Enter a valid YouTube video link.</p>}

      {videoId && (
        <>
          <div className="aspect-video overflow-hidden rounded-md bg-black">
            <div key={videoId} ref={playerElement} className="h-full w-full" />
          </div>
          {duration > 0 ? (
            <div className="space-y-4">
              <p className="text-xs text-muted-foreground">Preview position: {timeLabel(currentTime)} / {timeLabel(duration)}</p>
              <Button type="button" size="sm" variant="outline" onClick={() => player.current?.loadVideoById({ videoId, startSeconds, endSeconds })} disabled={endSeconds <= startSeconds}>
                Preview selected clip
              </Button>
              <div className="grid gap-4 sm:grid-cols-2">
                <div className="space-y-2">
                  <div className="flex items-center justify-between"><Label>Clip start</Label><span className="text-sm tabular-nums">{timeLabel(startSeconds)}</span></div>
                  <Slider min={0} max={Math.max(1, duration)} step={0.1} value={[startSeconds]} onValueChange={([value]) => setStartSeconds(Math.max(0, Math.min(value, endSeconds - 1)))} aria-label="Clip start time" />
                  <Button type="button" size="sm" variant="outline" onClick={() => setStartSeconds(Math.min(currentTime, endSeconds - 1))}>Set start to preview position</Button>
                </div>
                <div className="space-y-2">
                  <div className="flex items-center justify-between"><Label>Clip end</Label><span className="text-sm tabular-nums">{timeLabel(endSeconds)}</span></div>
                  <Slider min={0} max={Math.max(1, duration)} step={0.1} value={[endSeconds]} onValueChange={([value]) => setEndSeconds(Math.min(Math.max(value, startSeconds + 1), startSeconds + 30, duration))} aria-label="Clip end time" />
                  <Button type="button" size="sm" variant="outline" onClick={() => setEndSeconds(Math.min(currentTime, startSeconds + 30, duration))}>Set end to preview position</Button>
                </div>
              </div>
              <p className="text-xs text-muted-foreground">Selected clip: {(endSeconds - startSeconds).toFixed(1)} seconds (maximum 30 seconds).</p>
            </div>
          ) : <p className="text-sm text-muted-foreground">Load the video preview to choose your clip times.</p>}
        </>
      )}

      <div className="space-y-3 rounded-md bg-muted/50 p-3">
        <Label htmlFor="voice-source-media" className="flex items-center gap-2"><Film className="h-4 w-4" /> Source audio or video file</Label>
        <Input
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
        <p className="text-xs text-muted-foreground">Use media you own or are authorised to use. For YouTube content, supply a source file obtained through a permitted method. The full source is processed temporarily; only the selected segment is retained as the voice sample.</p>
        <label className="flex cursor-pointer items-start gap-2 text-sm">
          <input type="checkbox" checked={permissionConfirmed} onChange={(event) => setPermissionConfirmed(event.target.checked)} className="mt-1" />
          <span>I have permission to use this media and the speaker’s voice to create this voice profile.</span>
        </label>
        <Button type="button" onClick={extractSelectedClip} disabled={!videoId || !sourceFile || !permissionConfirmed || endSeconds <= startSeconds || endSeconds - startSeconds > 30 || isExtracting}>
          <Scissors className="mr-2 h-4 w-4" />{isExtracting ? 'Extracting clip…' : 'Extract selected audio'}
        </Button>
      </div>
      {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
    </div>
  );
}
