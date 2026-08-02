Pod::Spec.new do |s|
  s.name           = 'ChronicleMicControl'
  s.version        = '1.0.0'
  s.summary        = 'iOS microphone-mode info and far-field input tuning for Chronicle'
  s.description    = 'Reads AVCaptureDevice microphone modes (Voice Isolation / Wide Spectrum), opens the system mic-mode picker, and prefers an omnidirectional polar pattern on the built-in mic for far-field capture.'
  s.author         = 'Chronicle'
  s.homepage       = 'https://github.com/SimpleOpenSoftware/chronicle'
  s.license        = { :type => 'MIT' }
  s.platforms      = { :ios => '15.1' }
  s.source         = { :git => '' }
  s.static_framework = true
  s.dependency 'ExpoModulesCore'
  s.source_files   = '**/*.{h,m,swift}'
end
