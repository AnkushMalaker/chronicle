package com.chronicle.duplexaudio

import android.content.Context
import android.media.AcousticEchoCanceler
import android.media.AudioManager
import android.os.Build
import androidx.test.core.app.ApplicationProvider
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DuplexAudioInstrumentedTest {
  @Test fun api31CommunicationRoutingAndEffectProbeAreAvailable() {
    assertTrue(Build.VERSION.SDK_INT >= 31)
    val context = ApplicationProvider.getApplicationContext<Context>()
    val manager = context.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    assertNotNull(manager.availableCommunicationDevices)
    // The result may legitimately be false; probing must itself be safe on fallback devices.
    AcousticEchoCanceler.isAvailable()
  }
}
