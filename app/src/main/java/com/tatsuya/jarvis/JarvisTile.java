package com.tatsuya.jarvis;

import android.annotation.SuppressLint;
import android.content.Intent;
import android.graphics.drawable.Icon;
import android.os.Build;
import android.service.quicksettings.Tile;
import android.service.quicksettings.TileService;

/** クイック設定（画面上から引き下ろすパネル）の「ジャービス」ボタン。タップで聞き取り開始 */
public class JarvisTile extends TileService {

    @Override
    public void onStartListening() {
        Tile t = getQsTile();
        if (t == null) return;
        t.setState(Tile.STATE_ACTIVE);
        t.setLabel("ジャービス");
        if (Build.VERSION.SDK_INT >= 29) t.setSubtitle("話しかける");
        t.setIcon(Icon.createWithResource(this, R.drawable.ic_stat_jarvis));
        t.updateTile();
    }

    @SuppressLint("StartActivityAndCollapseDeprecated")
    @Override
    public void onClick() {
        if (Build.VERSION.SDK_INT >= 34) {
            startActivityAndCollapse(JarvisService.listenIntent(this));
        } else {
            Intent i = new Intent(this, MainActivity.class)
                    .setAction(MainActivity.ACTION_LISTEN)
                    .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK | Intent.FLAG_ACTIVITY_SINGLE_TOP);
            startActivityAndCollapse(i);
        }
    }
}
