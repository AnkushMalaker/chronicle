Pod::Spec.new do |s|
  s.name           = 'ChronicleDuplexAudio'
  s.version        = '1.0.0'
  s.summary        = 'Chronicle full-duplex capture and response playback'
  s.description    = 'One voice-processing AVAudioEngine graph for Chronicle protocol v1.'
  s.license        = { :type => 'MIT' }
  s.author         = 'Chronicle'
  s.homepage       = 'https://github.com/chronicle'
  s.platforms      = { :ios => '15.1' }
  s.swift_version  = '5.9'
  s.source         = { :path => '.' }
  s.static_framework = true
  s.dependency 'ExpoModulesCore'
  s.source_files = '**/*.swift'
  s.exclude_files = 'Tests/**/*.swift'
end
