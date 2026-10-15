import type { ReleaseInfo, ReleaseSource, SourceContext } from "../types.js";
import { isoDate } from "./util.js";

const DEFAULT_URL =
  "https://raw.githubusercontent.com/Spacegoose02/ankergames-lootden/main/ankergames.json";

interface HydraDownload {
  title?: string;
  uris?: string[];
  uploadDate?: string;
  fileSize?: string;
}

interface HydraCatalog {
  name?: string;
  downloads?: HydraDownload[];
}

function cleanTitle(raw: string): string {
  let title = raw.trim();
  title = title.replace(/\sv\d+(\.\d+)*/gi, " ");
  title = title.replace(/\s*\(.*?\)/g, " ");
  title = title.replace(/[._\-]/g, " ");
  return title.replace(/\s+/g, " ").trim();
}

export const ankerGames: ReleaseSource = {
  id: "ankergames",
  name: "AnkerGames",
  description: "Direct releases from Spacegoose02/ankergames-lootden catalog.",
  defaultBaseUrl: DEFAULT_URL,
  needsKey: false,
  limits: { perMinute: 60, perHour: 1000 },

  latest: async (ctx: SourceContext): Promise<ReleaseInfo[]> => {
    const feedUrl = ctx.config.baseUrl || DEFAULT_URL;

    console.log(`[AnkerGames] Fetching catalog from: ${feedUrl}`);

    const res = await ctx.client.fetch(feedUrl, {
      headers: {
        Accept: "application/json, text/plain",
        "User-Agent": "LootDen-Game-Manager",
      },
    });

    if (!res.ok) {
      console.error(`[AnkerGames] Failed to fetch feed (${res.status} ${res.statusText})`);
      return [];
    }

    const catalog = (await res.json()) as HydraCatalog;
    const items = catalog.downloads ?? [];

    console.log(`[AnkerGames] Loaded ${items.length} releases from catalog.`);

    const releases: ReleaseInfo[] = [];

    for (let idx = 0; idx < items.length; idx++) {
      const item = items[idx];
      if (!item || !item.title) continue;

      const displayTitle = cleanTitle(item.title);
      const mainUrl = item.uris && item.uris.length > 0 ? item.uris[0] : null;
      const size = item.fileSize;
      const date = item.uploadDate ? isoDate(item.uploadDate) : isoDate(new Date().toISOString());

      const details: Record<string, string> = {
        Source: "AnkerGames",
        OriginalTitle: item.title,
      };
      if (size) details["Size"] = size;

      releases.push({
        id: `anker:${idx}:${displayTitle.toLowerCase().replace(/\s+/g, "-")}`,
        title: displayTitle || item.title,
        date,
        platform: "pc",
        url: mainUrl,
        image: null,
        summary: size ? `${item.title} (${size})` : item.title,
        details,
      });
    }

    return releases;
  },

  search: async (ctx: SourceContext, query: string): Promise<ReleaseInfo[]> => {
    const all = await ankerGames.latest(ctx);
    const q = query.toLowerCase().trim();
    return all.filter((r) => r.title.toLowerCase().includes(q));
  },
};
