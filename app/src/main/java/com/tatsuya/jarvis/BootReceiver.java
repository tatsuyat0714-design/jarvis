package com.tatsuya.jarvis;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/** スマホを再起動したとき、常駐がONなら自動で待機に戻る */
public class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context c, Intent intent) {
        String a = intent.getAction();
        if (!Intent.ACTION_BOOT_COMPLETED.equals(a) && !Intent.ACTION_MY_PACKAGE_REPLACED.equals(a)) return;
        if ("1".equals(c.getSharedPreferences("jarvis", Context.MODE_PRIVATE).getString("resident", "0"))) {
            try { JarvisService.start(c); } catch (Exception ignored) {}
        }
    }
}
