# iPhone to Immich on the tower (Hidden album stays on the phone)

## Why Hidden stays put

Immich's iOS app asks iOS for photos with `includeHiddenAssets = false` on every library fetch
(`mobile/ios/Runner/Sync/MessagesImpl.swift` in immich-app/immich, main branch as of 2026-11-10),
and iOS leaves hidden photos out by default anyway. So the app never sees the Hidden album, and
can't back it up. The docs don't spell this out, so step 4 checks it on your phone before anything uploads.

## Steps on the iPhone

1. **Lock Hidden (extra safety):** Settings > Apps > Photos (Settings > Photos before iOS 18) >
   turn on **Use Face ID**.
2. **Reach the tower:** at home, Wi-Fi is enough. Away from home, install Tailscale and sign in to
   your existing tailnet.
3. **Install Immich** from the App Store. Server URL: `http://<tower-tailscale-ip>:2283`
   (the install script prints it). Log in with the admin account you made on the tower.
   When iOS asks for photo access, choose **Allow Full Access**.
4. **Check before backing up (go/no-go):**
   - In Photos, hide one throwaway photo (a screenshot).
   - In Immich, open Library > On this device > **Recents** and scroll to the newest items.
   - **Go** if the throwaway is not there. **No-go** if it is: stop and tell me. Nothing has uploaded yet.
5. **Pick what to back up:** Backup > Backup albums > tap **Recents** (all your photos, minus Hidden and
   Recently Deleted). Leave everything else as is.
6. **Leave these as they are:** "Wi-Fi only" on, and any option that frees up space on the phone off.
   The phone keeps every original.
7. **First backup:** start it on Wi-Fi with the phone charging and Immich open. Around 3,000 photos takes a while.
   iOS decides when background backups run.
8. **Done check:** on the tower, open the Immich web page and check the newest photo matches your camera roll,
   and the throwaway from step 4 isn't there. Then unhide or delete the throwaway.

Then on the tower: `bash scout_immich.sh`
